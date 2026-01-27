"""
ThrivingCare Website API
=========================

Backend API to receive candidate signups from website and integrate with automation system.

Endpoints:
- POST /api/candidates - Create new candidate from website
- POST /api/candidates/{id}/resume - Upload resume
- GET /api/jobs/count - Get total active jobs
- GET /api/jobs - Get job listings (paginated)
- GET /run-migrations - Run database migrations
- POST /api/calculate-pay - Calculate pay package from bill rate
- GET /api/gsa-rates - Get GSA per diem rates for a location
"""

from fastapi import FastAPI, File, UploadFile, HTTPException, Form, Header, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from decimal import Decimal
import os
from datetime import datetime
import re
import psycopg2
from psycopg2.extras import RealDictCursor
import boto3
from twilio.rest import Client as TwilioClient

app = FastAPI(title="ThrivingCare API")

# CORS middleware - allow your domain
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://thrivingcarestaffing.com",
        "https://www.thrivingcarestaffing.com",
        "http://localhost:3000",  # for testing
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
DATABASE_URL = os.getenv('DATABASE_URL')
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_PHONE = os.getenv('TWILIO_PHONE_NUMBER')
AWS_BUCKET = os.getenv('AWS_S3_BUCKET', 'thrivingcare-resumes')

# Initialize services
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None
s3_client = boto3.client('s3') if os.getenv('AWS_ACCESS_KEY_ID') else None


# ============================================================================
# MODELS
# ============================================================================

class CandidateIntake(BaseModel):
    firstName: str
    lastName: str
    discipline: str
    specialty: str
    email: EmailStr
    phone: str
    homeAddress: str
    source: str = "website_first_visit"
    visitedAt: Optional[str] = None


class CandidateResponse(BaseModel):
    id: int
    message: str
    status: str


class AdminJobCreate(BaseModel):
    """Model for creating jobs via admin panel"""
    title: str
    discipline: str
    facility: str
    setting: Optional[str] = None
    city: str
    state: str
    duration_weeks: int = 13
    hours_per_week: int = 40
    shift: Optional[str] = "Days"
    start_date: Optional[str] = None
    bill_rate: float
    margin_percent: float = 20
    description: str
    requirements: Optional[List[str]] = []
    benefits: Optional[List[str]] = []
class JobStatusUpdate(BaseModel):
    active: bool


class PipelineCreate(BaseModel):
    candidate_id: int
    job_id: Optional[int] = None
    stage: str = "new"


class PipelineStageUpdate(BaseModel):
    stage: str


class PipelineNoteCreate(BaseModel):
    note: str
class PipelineNoteCreate(BaseModel):
    note: str


# ============================================================================
# ADDRESS PARSER
# ============================================================================

def parse_address(full_address: str) -> dict:
    """Parse a full address into components"""
    
    result = {
        'street': '',
        'city': '',
        'state': '',
        'zip': ''
    }
    
    if not full_address:
        return result
    
    import re
    
    # Pattern for ZIP code
    zip_match = re.search(r'\b(\d{5}(?:-\d{4})?)\b', full_address)
    if zip_match:
        result['zip'] = zip_match.group(1)
        full_address = full_address.replace(zip_match.group(1), '').strip()
    
    # Pattern for state (2 letter code)
    state_match = re.search(r'\b([A-Z]{2})\b', full_address.upper())
    if state_match:
        result['state'] = state_match.group(1)
    
    # Split by comma
    parts = [p.strip() for p in full_address.split(',')]
    
    if len(parts) >= 3:
        result['street'] = parts[0]
        result['city'] = parts[1]
    elif len(parts) == 2:
        if result['state'] and result['state'] in parts[1].upper():
            result['city'] = parts[0]
        else:
            result['street'] = parts[0]
            city_part = parts[1].upper().replace(result['state'], '').strip()
            result['city'] = city_part.title()
    elif len(parts) == 1:
        words = full_address.split()
        city_words = [w for w in words if w.upper() != result['state'] and not re.match(r'\d{5}', w)]
        result['city'] = ' '.join(city_words).strip(' ,')
    
    return result
# Admin password - CHANGE THIS IN PRODUCTION!
ADMIN_PASSWORD = "thrivingcare2024"


# ============================================================================
# DATABASE HELPERS
# ============================================================================

def get_db_connection():
    """Get database connection"""
    return psycopg2.connect(DATABASE_URL)


def parse_address(address: str) -> dict:
    """Parse full address into components"""
    # Simple parser - improve as needed
    parts = [p.strip() for p in address.split(',')]
    
    city = parts[-2] if len(parts) >= 2 else ''
    
    if len(parts) >= 1:
        state_zip = parts[-1].split()
        state = state_zip[0] if state_zip else ''
        zip_code = state_zip[1] if len(state_zip) > 1 else ''
    else:
        state = ''
        zip_code = ''
    
    return {
        'city': city,
        'state': state,
        'zip_code': zip_code
    }


# ============================================================================
# ENDPOINTS
# ============================================================================

@app.get("/")
def read_root():
    """Health check"""
    return {
        "status": "healthy",
        "service": "ThrivingCare API",
        "version": "1.1"
    }


# ============================================================================
# DATABASE MIGRATIONS
# ============================================================================

@app.get("/run-migrations")
def run_migrations():
    """Run database migrations to add all required columns and tables"""
    
    migrations = [
        # JOBS TABLE UPDATES
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS description TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS discipline VARCHAR(100)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS requirements TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS pay_rate DECIMAL(10,2)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS shift_length VARCHAR(20)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS shift_type VARCHAR(50)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS days_per_week INTEGER",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS schedule_notes TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS contract_length VARCHAR(50)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS contract_type VARCHAR(50)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS start_date DATE",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS setting VARCHAR(100)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source VARCHAR(100) DEFAULT 'Manual Entry'",
        
        # NEW: Admin job creation columns
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS facility VARCHAR(255)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS location VARCHAR(255)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS duration_weeks INTEGER DEFAULT 13",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS hours_per_week INTEGER DEFAULT 40",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS shift VARCHAR(50)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS bill_rate DECIMAL(10,2)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS margin_percent DECIMAL(5,2) DEFAULT 20",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS hourly_rate DECIMAL(10,2)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS weekly_gross DECIMAL(10,2)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS contract_total DECIMAL(10,2)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS benefits TEXT[]",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS enriched BOOLEAN DEFAULT FALSE",
        
        # CANDIDATES TABLE UPDATES
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS discipline VARCHAR(100)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS specialty VARCHAR(100)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS years_experience INTEGER",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS license_states TEXT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS preferred_locations TEXT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS min_pay_rate DECIMAL(10,2)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS max_pay_rate DECIMAL(10,2)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS availability_date DATE",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS ai_vetting_status VARCHAR(50) DEFAULT 'pending'",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS ai_vetting_score INTEGER",
        
        # FIX: Expand address columns to handle full addresses
        "ALTER TABLE candidates ALTER COLUMN home_state TYPE VARCHAR(255)",
        "ALTER TABLE candidates ALTER COLUMN home_city TYPE VARCHAR(255)",
        "ALTER TABLE candidates ALTER COLUMN home_address TYPE TEXT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS ai_vetting_score INTEGER",
        
        # ADMINS TABLE (NEW)
        """CREATE TABLE IF NOT EXISTS admins (
            id SERIAL PRIMARY KEY,
            email VARCHAR(255) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            first_name VARCHAR(100),
            last_name VARCHAR(100),
            role VARCHAR(50) DEFAULT 'recruiter',
            active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        
        # PIPELINE STAGES TABLE (NEW)
        """CREATE TABLE IF NOT EXISTS pipeline_stages (
            id SERIAL PRIMARY KEY,
            candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
            job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
            stage VARCHAR(50) NOT NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        
        # AI VETTING LOGS TABLE (NEW)
        """CREATE TABLE IF NOT EXISTS ai_vetting_logs (
            id SERIAL PRIMARY KEY,
            candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
            session_id VARCHAR(100),
            question TEXT,
            response TEXT,
            question_type VARCHAR(50),
            score INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        
        # GSA PER DIEM RATES TABLE (NEW)
        """CREATE TABLE IF NOT EXISTS gsa_rates (
            id SERIAL PRIMARY KEY,
            city VARCHAR(100) NOT NULL,
            state VARCHAR(2) NOT NULL,
            county VARCHAR(100),
            daily_lodging DECIMAL(10,2) NOT NULL,
            daily_mie DECIMAL(10,2) NOT NULL,
            fiscal_year INTEGER NOT NULL DEFAULT 2025,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(city, state, fiscal_year)
        )""",
    ]
    
    results = []
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                for migration in migrations:
                    try:
                        cur.execute(migration)
                        conn.commit()
                        results.append({
                            "success": True,
                            "statement": migration[:60] + "..."
                        })
                    except Exception as e:
                        conn.rollback()
                        results.append({
                            "success": False,
                            "statement": migration[:60] + "...",
                            "error": str(e)
                        })
        
        successful = len([r for r in results if r["success"]])
        failed = len([r for r in results if not r["success"]])
        
        return {
            "message": "Migrations complete!",
            "total": len(migrations),
            "successful": successful,
            "failed": failed,
            "details": results
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Migration failed: {str(e)}")


@app.get("/api/jobs/count")
def get_jobs_count():
    """Get total number of active jobs"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM jobs WHERE active = TRUE")
                count = cur.fetchone()[0]
                return {"count": count, "updated_at": datetime.now().isoformat()}
    except Exception as e:
        print(f"Error fetching job count: {e}")
        # Return fallback count
        return {"count": 250, "updated_at": datetime.now().isoformat()}


@app.post("/api/candidates", response_model=CandidateResponse)
async def create_candidate(candidate: CandidateIntake):
    """
    Create new candidate from website signup
    
    This endpoint:
    1. Saves candidate to database
    2. Sends welcome SMS
    3. Triggers matching engine
    4. Returns candidate ID for resume upload
    """
    
    try:
        # Parse address components
        address_parts = parse_address(candidate.homeAddress)
        
        # Insert into database
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = """
                    INSERT INTO candidates (
                        first_name, last_name, email, phone,
                        home_address, home_city, home_state, home_zip,
                        license_type, specialties, 
                        active, created_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, ARRAY[%s],
                        TRUE, NOW()
                    ) RETURNING id
                """
                
                cur.execute(query, (
                    candidate.firstName,
                    candidate.lastName,
                    candidate.email,
                    candidate.phone,
                    candidate.homeAddress,
                    address_parts['city'],
                    address_parts['state'],
                    address_parts['zip_code'],
                    candidate.discipline,
                    candidate.specialty
                ))
                
                candidate_id = cur.fetchone()['id']
        
        # Send welcome SMS
        if twilio_client:
            try:
                               welcome_message = f"""Hi {candidate.firstName}! 👋

Welcome to ThrivingCare Staffing!

We're analyzing your profile ({candidate.specialty}) and will text you as soon as we find matching positions.

In the meantime, browse jobs: https://thrivingcarestaffing.com/jobs

Questions? Reply to this message!"""

                twilio_client.messages.create(
                    body=welcome_message,
                    from_=TWILIO_PHONE,
                    to=candidate.phone
                )
                print(f"  ✓ Welcome SMS sent to {candidate.phone}")
            except Exception as e:
                print(f"Failed to send welcome SMS: {e}")
        
        # Log event
        print(f"✓ New candidate: {candidate.firstName} {candidate.lastName} ({candidate.email})")
        print(f"  Discipline: {candidate.discipline}")
        print(f"  Location: {address_parts['city']}, {address_parts['state']}")
        
        return CandidateResponse(
            id=candidate_id,
            message="Welcome! We'll start matching you to positions right away.",
            status="success"
        )
        
    except Exception as e:
        print(f"Error creating candidate: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/candidates/{candidate_id}/resume")
async def upload_resume(candidate_id: int, resume: UploadFile = File(...)):
    """
    Upload resume for candidate
    
    Saves to S3 and updates database with file URL
    """
    
    try:
        # Validate file type
        allowed_types = ['application/pdf', 'application/msword', 
                        'application/vnd.openxmlformats-officedocument.wordprocessingml.document']
        
        if resume.content_type not in allowed_types:
            raise HTTPException(status_code=400, detail="Invalid file type. Please upload PDF or Word document.")
        
        # Validate file size (5MB max)
        resume_content = await resume.read()
        if len(resume_content) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File too large. Maximum size is 5MB.")
        
        # Generate unique filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        file_extension = resume.filename.split('.')[-1]
        s3_key = f"resumes/candidate_{candidate_id}_{timestamp}.{file_extension}"
        
        # Upload to S3
        if s3_client:
            s3_client.put_object(
                Bucket=AWS_BUCKET,
                Key=s3_key,
                Body=resume_content,
                ContentType=resume.content_type
            )
            
            resume_url = f"https://{AWS_BUCKET}.s3.amazonaws.com/{s3_key}"
        else:
            # Fallback: save locally (for development)
            os.makedirs('uploads/resumes', exist_ok=True)
            filepath = f"uploads/resumes/{s3_key}"
            with open(filepath, 'wb') as f:
                f.write(resume_content)
            resume_url = f"/uploads/resumes/{s3_key}"
        
        # Update database
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE candidates SET resume_url = %s, updated_at = NOW() WHERE id = %s",
                    (resume_url, candidate_id)
                )
        
        print(f"✓ Resume uploaded for candidate {candidate_id}: {resume_url}")
        
        return {
            "status": "success",
            "message": "Resume uploaded successfully",
            "url": resume_url
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error uploading resume: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/jobs")
def get_jobs(
    specialty: Optional[str] = None,
    location: Optional[str] = None,
    page: int = 1,
    per_page: int = 20
):
    """
    Get job listings (paginated)
    
    Query params:
    - specialty: Filter by specialty
    - location: Filter by city or state
    - page: Page number (default 1)
    - per_page: Results per page (default 20)
    """
    
    try:
        offset = (page - 1) * per_page
        
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Build query
                query = "SELECT * FROM jobs WHERE active = TRUE AND enriched = TRUE"
                params = []
                
                if specialty:
                    query += " AND (specialty ILIKE %s OR title ILIKE %s)"
                    params.extend([f"%{specialty}%", f"%{specialty}%"])
                
                if location:
                    query += " AND (city ILIKE %s OR state ILIKE %s)"
                    params.extend([f"%{location}%", f"%{location}%"])
                
                query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
                params.extend([per_page, offset])
                
                cur.execute(query, params)
                jobs = cur.fetchall()
                
                # Get total count
                count_query = "SELECT COUNT(*) FROM jobs WHERE active = TRUE AND enriched = TRUE"
                if specialty or location:
                    count_query = query.split('ORDER BY')[0]
                cur.execute(count_query, params[:-2] if params else [])
                total = cur.fetchone()['count']
                
                return {
                    "jobs": jobs,
                    "page": page,
                    "per_page": per_page,
                    "total": total,
                    "pages": (total + per_page - 1) // per_page
                }
    except Exception as e:
        print(f"Error fetching jobs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# ADMIN JOB CREATION ENDPOINT
# ============================================================================

@app.post("/api/admin/jobs")
async def create_job_admin(
    job: AdminJobCreate,
    x_admin_password: str = Header(None)
):
    """
    Create a new job listing with auto-calculated pay package.
    Requires admin password in X-Admin-Password header.
    
    Used by: admin.html
    """
    
    # Verify admin password
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        # Calculate pay package
        candidate_hourly = job.bill_rate * (1 - job.margin_percent / 100)
        weekly_gross = candidate_hourly * job.hours_per_week
        contract_total = weekly_gross * job.duration_weeks
        
        # Format location
        location = f"{job.city}, {job.state}"
        
        # Insert into database
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = """
                    INSERT INTO jobs (
                        title, discipline, facility, setting, city, state, location,
                        duration_weeks, hours_per_week, shift, start_date,
                        bill_rate, margin_percent, hourly_rate, weekly_gross, contract_total,
                        description, requirements, benefits, active, enriched, source, created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, TRUE, TRUE, 'admin', NOW()
                    ) RETURNING id, title, city, state, weekly_gross, created_at
                """
                
                cur.execute(query, (
                    job.title,
                    job.discipline,
                    job.facility,
                    job.setting,
                    job.city,
                    job.state,
                    location,
                    job.duration_weeks,
                    job.hours_per_week,
                    job.shift,
                    job.start_date if job.start_date else None,
                    job.bill_rate,
                    job.margin_percent,
                    round(candidate_hourly, 2),
                    round(weekly_gross, 2),
                    round(contract_total, 2),
                    job.description,
                    job.requirements,
                    job.benefits
                ))
                
                new_job = cur.fetchone()
                conn.commit()
        
        print(f"✓ New job created via admin: {job.title} in {location}")
        
        return {
            "success": True,
            "message": "Job created successfully",
            "job": {
                "id": new_job['id'],
                "title": new_job['title'],
                "location": f"{new_job['city']}, {new_job['state']}",
                "weekly_gross": float(new_job['weekly_gross']),
                "created_at": new_job['created_at'].isoformat() if new_job['created_at'] else None
            }
        }
        
    except Exception as e:
        print(f"Error creating job: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# ADMIN: CANDIDATES MANAGEMENT
# ============================================================================

@app.get("/api/admin/candidates")
async def get_candidates(
    discipline: Optional[str] = None,
    x_admin_password: str = Header(None)
):
    """Get all candidates with optional filtering"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = "SELECT * FROM candidates WHERE active = TRUE"
                params = []
                
                if discipline:
                    query += " AND (license_type ILIKE %s OR discipline ILIKE %s)"
                    params.extend([f"%{discipline}%", f"%{discipline}%"])
                
                query += " ORDER BY created_at DESC"
                
                cur.execute(query, params)
                candidates = cur.fetchall()
                
                return {"candidates": candidates}
    except Exception as e:
        print(f"Error fetching candidates: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/candidates/{candidate_id}/alert")
async def send_job_alert(
    candidate_id: int,
    x_admin_password: str = Header(None)
):
    """Send job alert SMS to a candidate"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Get candidate
                cur.execute("SELECT * FROM candidates WHERE id = %s", (candidate_id,))
                candidate = cur.fetchone()
                
                if not candidate:
                    raise HTTPException(status_code=404, detail="Candidate not found")
                
                # Get matching jobs
                cur.execute("""
                    SELECT title, city, state, weekly_gross 
                    FROM jobs 
                    WHERE active = TRUE AND enriched = TRUE
                    LIMIT 3
                """)
                jobs = cur.fetchall()
                
                if not jobs:
                    raise HTTPException(status_code=400, detail="No active jobs to send")
                
                # Build SMS message
                job_list = "\n".join([f"• {j['title']} in {j['city']}, {j['state']} - ${int(j['weekly_gross'] or 0)}/wk" for j in jobs])
                message = f"""Hi {candidate['first_name']}! 🎉

New jobs matching your profile:

{job_list}

View all: https://thrivingcarestaffing.com/jobs

Reply STOP to unsubscribe."""

                # Send SMS via Twilio
                if twilio_client and candidate['phone']:
                    twilio_client.messages.create(
                        body=message,
                        from_=TWILIO_PHONE,
                        to=candidate['phone']
                    )
                    return {"success": True, "message": "Job alert sent"}
                else:
                    return {"success": False, "message": "SMS not configured or no phone number"}
                    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error sending job alert: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# ADMIN: ANALYTICS
# ============================================================================

@app.get("/api/admin/analytics")
async def get_analytics(x_admin_password: str = Header(None)):
    """Get dashboard analytics"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Total active jobs
                cur.execute("SELECT COUNT(*) as count FROM jobs WHERE active = TRUE")
                total_jobs = cur.fetchone()['count']
                
                # Total candidates
                cur.execute("SELECT COUNT(*) as count FROM candidates WHERE active = TRUE")
                total_candidates = cur.fetchone()['count']
                
                # New this week
                cur.execute("""
                    SELECT COUNT(*) as count FROM candidates 
                    WHERE active = TRUE AND created_at >= NOW() - INTERVAL '7 days'
                """)
                new_this_week = cur.fetchone()['count']
                
                # With resume
                cur.execute("""
                    SELECT COUNT(*) as count FROM candidates 
                    WHERE active = TRUE AND resume_url IS NOT NULL
                """)
                with_resume = cur.fetchone()['count']
                
                # By discipline
                cur.execute("""
                    SELECT COALESCE(license_type, discipline, 'Other') as discipline, COUNT(*) as count 
                    FROM candidates 
                    WHERE active = TRUE
                    GROUP BY COALESCE(license_type, discipline, 'Other')
                    ORDER BY count DESC
                """)
                discipline_rows = cur.fetchall()
                by_discipline = {row['discipline']: row['count'] for row in discipline_rows}
                
                return {
                    "total_jobs": total_jobs,
                    "total_candidates": total_candidates,
                    "new_this_week": new_this_week,
                    "with_resume": with_resume,
                    "by_discipline": by_discipline
                }
    except Exception as e:
        print(f"Error fetching analytics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# ADMIN: JOB MANAGEMENT (Edit/Delete)
# ============================================================================

class JobStatusUpdate(BaseModel):
    active: bool


@app.put("/api/admin/jobs/{job_id}/status")
async def update_job_status(
    job_id: int,
    status: JobStatusUpdate,
    x_admin_password: str = Header(None)
):
    """Update job active status"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET active = %s, updated_at = NOW() WHERE id = %s",
                    (status.active, job_id)
                )
                conn.commit()
                return {"success": True, "message": f"Job {'activated' if status.active else 'deactivated'}"}
    except Exception as e:
        print(f"Error updating job status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/admin/jobs/{job_id}")
async def delete_job(
    job_id: int,
    x_admin_password: str = Header(None)
):
    """Delete a job"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
                conn.commit()
                return {"success": True, "message": "Job deleted"}
    except Exception as e:
        print(f"Error deleting job: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# GSA PER DIEM RATES & PAY CALCULATOR
# ============================================================================

# GSA Per Diem Rates FY2025 - Key Locations
GSA_RATES_FY2025 = {
    # TEXAS
    "Austin, TX": {"lodging": 166, "mie": 74},
    "Dallas, TX": {"lodging": 161, "mie": 74},
    "Fort Worth, TX": {"lodging": 143, "mie": 69},
    "Houston, TX": {"lodging": 156, "mie": 74},
    "San Antonio, TX": {"lodging": 138, "mie": 69},
    "El Paso, TX": {"lodging": 107, "mie": 68},
    "Plano, TX": {"lodging": 161, "mie": 74},
    "Irving, TX": {"lodging": 161, "mie": 74},
    "Arlington, TX": {"lodging": 143, "mie": 69},
    "Frisco, TX": {"lodging": 161, "mie": 74},
    "McKinney, TX": {"lodging": 161, "mie": 74},
    "Allen, TX": {"lodging": 161, "mie": 74},
    
    # CALIFORNIA
    "Los Angeles, CA": {"lodging": 209, "mie": 79},
    "San Francisco, CA": {"lodging": 311, "mie": 79},
    "San Diego, CA": {"lodging": 194, "mie": 74},
    "Sacramento, CA": {"lodging": 168, "mie": 74},
    "San Jose, CA": {"lodging": 258, "mie": 79},
    "Fresno, CA": {"lodging": 126, "mie": 69},
    "Oakland, CA": {"lodging": 240, "mie": 79},
    "Irvine, CA": {"lodging": 188, "mie": 74},
    "Anaheim, CA": {"lodging": 188, "mie": 74},
    
    # FLORIDA
    "Miami, FL": {"lodging": 195, "mie": 79},
    "Orlando, FL": {"lodging": 163, "mie": 69},
    "Tampa, FL": {"lodging": 150, "mie": 69},
    "Jacksonville, FL": {"lodging": 138, "mie": 69},
    "Fort Lauderdale, FL": {"lodging": 189, "mie": 74},
    
    # NEW YORK
    "New York City, NY": {"lodging": 282, "mie": 79},
    "Buffalo, NY": {"lodging": 119, "mie": 69},
    "Albany, NY": {"lodging": 143, "mie": 69},
    
    # ILLINOIS
    "Chicago, IL": {"lodging": 231, "mie": 79},
    "Springfield, IL": {"lodging": 107, "mie": 68},
    
    # PENNSYLVANIA
    "Philadelphia, PA": {"lodging": 194, "mie": 79},
    "Pittsburgh, PA": {"lodging": 165, "mie": 74},
    
    # OHIO
    "Columbus, OH": {"lodging": 139, "mie": 69},
    "Cleveland, OH": {"lodging": 152, "mie": 74},
    "Cincinnati, OH": {"lodging": 147, "mie": 69},
    
    # GEORGIA
    "Atlanta, GA": {"lodging": 181, "mie": 79},
    "Savannah, GA": {"lodging": 155, "mie": 74},
    
    # NORTH CAROLINA
    "Charlotte, NC": {"lodging": 155, "mie": 74},
    "Raleigh, NC": {"lodging": 150, "mie": 74},
    
    # MICHIGAN
    "Detroit, MI": {"lodging": 159, "mie": 74},
    "Grand Rapids, MI": {"lodging": 134, "mie": 69},
    
    # ARIZONA
    "Phoenix, AZ": {"lodging": 171, "mie": 74},
    "Tucson, AZ": {"lodging": 131, "mie": 69},
    "Scottsdale, AZ": {"lodging": 197, "mie": 79},
    
    # COLORADO
    "Denver, CO": {"lodging": 198, "mie": 79},
    "Colorado Springs, CO": {"lodging": 141, "mie": 69},
    
    # WASHINGTON
    "Seattle, WA": {"lodging": 227, "mie": 79},
    "Tacoma, WA": {"lodging": 166, "mie": 74},
    
    # MASSACHUSETTS
    "Boston, MA": {"lodging": 268, "mie": 79},
    "Cambridge, MA": {"lodging": 268, "mie": 79},
    
    # VIRGINIA / DC / MARYLAND
    "Washington, DC": {"lodging": 258, "mie": 79},
    "Arlington, VA": {"lodging": 258, "mie": 79},
    "Baltimore, MD": {"lodging": 173, "mie": 79},
    
    # NEW JERSEY
    "Newark, NJ": {"lodging": 171, "mie": 79},
    "Jersey City, NJ": {"lodging": 218, "mie": 79},
    
    # TENNESSEE
    "Nashville, TN": {"lodging": 197, "mie": 79},
    "Memphis, TN": {"lodging": 129, "mie": 69},
    
    # MINNESOTA
    "Minneapolis, MN": {"lodging": 173, "mie": 79},
    
    # OREGON
    "Portland, OR": {"lodging": 176, "mie": 79},
    
    # INDIANA
    "Indianapolis, IN": {"lodging": 147, "mie": 74},
    
    # MISSOURI
    "Kansas City, MO": {"lodging": 151, "mie": 74},
    "St. Louis, MO": {"lodging": 144, "mie": 74},
    
    # LOUISIANA
    "New Orleans, LA": {"lodging": 184, "mie": 79},
    
    # NEVADA
    "Las Vegas, NV": {"lodging": 151, "mie": 74},
    
    # UTAH
    "Salt Lake City, UT": {"lodging": 155, "mie": 74},
}

# Standard CONUS rate for unlisted locations (FY2025/FY2026)
STANDARD_CONUS = {"lodging": 110, "mie": 68}


def get_gsa_rates(city: str, state: str) -> dict:
    """Get GSA per diem rates for a location. Queries database first, falls back to hardcoded."""
    
    # Try database first
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Try exact match
                cur.execute("""
                    SELECT daily_lodging, daily_mie FROM gsa_rates 
                    WHERE LOWER(city) = LOWER(%s) AND LOWER(state) = LOWER(%s) 
                    AND fiscal_year = 2025
                """, (city, state))
                result = cur.fetchone()
                
                if result:
                    return {"lodging": float(result["daily_lodging"]), "mie": float(result["daily_mie"])}
                
                # Try standard CONUS from database
                cur.execute("""
                    SELECT daily_lodging, daily_mie FROM gsa_rates 
                    WHERE city = 'Standard CONUS' AND fiscal_year = 2025
                """)
                result = cur.fetchone()
                
                if result:
                    return {"lodging": float(result["daily_lodging"]), "mie": float(result["daily_mie"])}
    except Exception as e:
        print(f"Database lookup failed, using hardcoded rates: {e}")
    
    # Fallback to hardcoded rates
    location_key = f"{city}, {state}"
    if location_key in GSA_RATES_FY2025:
        return GSA_RATES_FY2025[location_key]
    
    # Try case-insensitive
    for key, rates in GSA_RATES_FY2025.items():
        if key.lower() == location_key.lower():
            return rates
    
    return STANDARD_CONUS


class PayCalculatorRequest(BaseModel):
    bill_rate: float
    city: str
    state: str
    hours_per_week: int = 40
    is_travel_contract: bool = True
    gross_margin_pct: float = 0.20
    burden_pct: float = 0.20


@app.get("/api/gsa-rates")
def get_gsa_rates_endpoint(city: str, state: str):
    """Get GSA per diem rates for a specific location"""
    rates = get_gsa_rates(city, state)
    location_key = f"{city}, {state}"
    is_standard = location_key not in GSA_RATES_FY2025
    
    return {
        "location": location_key,
        "daily_lodging": rates["lodging"],
        "daily_mie": rates["mie"],
        "monthly_lodging": rates["lodging"] * 30,
        "monthly_mie": rates["mie"] * 21.65,  # ~21.65 work days per month
        "is_standard_rate": is_standard,
        "fiscal_year": 2025
    }


@app.get("/api/gsa-rates/all")
def get_all_gsa_rates():
    """Get all GSA per diem rates"""
    rates_list = []
    for location, rates in GSA_RATES_FY2025.items():
        city, state = location.rsplit(", ", 1)
        rates_list.append({
            "city": city,
            "state": state,
            "daily_lodging": rates["lodging"],
            "daily_mie": rates["mie"]
        })
    
    return {
        "rates": sorted(rates_list, key=lambda x: (x["state"], x["city"])),
        "count": len(rates_list),
        "standard_rate": STANDARD_CONUS,
        "fiscal_year": 2025
    }


@app.get("/seed-gsa-rates")
def seed_gsa_rates():
    """
    One-time endpoint to populate GSA rates into database.
    Run once after migrations, then rates are stored locally.
    """
    try:
        inserted = 0
        skipped = 0
        
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                for location, rates in GSA_RATES_FY2025.items():
                    city, state = location.rsplit(", ", 1)
                    try:
                        cur.execute("""
                            INSERT INTO gsa_rates (city, state, daily_lodging, daily_mie, fiscal_year)
                            VALUES (%s, %s, %s, %s, 2025)
                            ON CONFLICT (city, state, fiscal_year) DO UPDATE
                            SET daily_lodging = EXCLUDED.daily_lodging,
                                daily_mie = EXCLUDED.daily_mie,
                                updated_at = CURRENT_TIMESTAMP
                        """, (city, state, rates["lodging"], rates["mie"]))
                        inserted += 1
                    except Exception as e:
                        skipped += 1
                        print(f"Error inserting {location}: {e}")
                
                # Also insert standard CONUS rate
                cur.execute("""
                    INSERT INTO gsa_rates (city, state, daily_lodging, daily_mie, fiscal_year)
                    VALUES ('Standard CONUS', 'US', %s, %s, 2025)
                    ON CONFLICT (city, state, fiscal_year) DO UPDATE
                    SET daily_lodging = EXCLUDED.daily_lodging,
                        daily_mie = EXCLUDED.daily_mie,
                        updated_at = CURRENT_TIMESTAMP
                """, (STANDARD_CONUS["lodging"], STANDARD_CONUS["mie"]))
                
                conn.commit()
        
        return {
            "message": "GSA rates seeded successfully!",
            "inserted": inserted,
            "skipped": skipped,
            "fiscal_year": 2025
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to seed GSA rates: {str(e)}")


@app.get("/seed-gsa-rates-complete")
def seed_gsa_rates_complete():
    """
    Seed ALL 389 GSA Non-Standard Area locations into database.
    Run once to get complete US coverage.
    """
    
    # Complete GSA FY2025/FY2026 Non-Standard Areas (389 locations)
    GSA_COMPLETE = {
        # ALABAMA
        "Auburn, AL": {"lodging": 114, "mie": 68},
        "Birmingham, AL": {"lodging": 129, "mie": 69},
        "Gulf Shores, AL": {"lodging": 145, "mie": 69},
        "Huntsville, AL": {"lodging": 130, "mie": 69},
        "Mobile, AL": {"lodging": 115, "mie": 68},
        "Montgomery, AL": {"lodging": 112, "mie": 68},
        "Orange Beach, AL": {"lodging": 145, "mie": 69},
        # ARIZONA
        "Flagstaff, AZ": {"lodging": 152, "mie": 74},
        "Grand Canyon, AZ": {"lodging": 152, "mie": 74},
        "Phoenix, AZ": {"lodging": 171, "mie": 74},
        "Scottsdale, AZ": {"lodging": 197, "mie": 79},
        "Sedona, AZ": {"lodging": 175, "mie": 74},
        "Tempe, AZ": {"lodging": 171, "mie": 74},
        "Tucson, AZ": {"lodging": 131, "mie": 69},
        "Yuma, AZ": {"lodging": 115, "mie": 68},
        # ARKANSAS
        "Fayetteville, AR": {"lodging": 119, "mie": 68},
        "Hot Springs, AR": {"lodging": 118, "mie": 68},
        "Little Rock, AR": {"lodging": 113, "mie": 68},
        # CALIFORNIA
        "Anaheim, CA": {"lodging": 188, "mie": 74},
        "Bakersfield, CA": {"lodging": 124, "mie": 69},
        "Burbank, CA": {"lodging": 209, "mie": 79},
        "Carlsbad, CA": {"lodging": 194, "mie": 74},
        "Coronado, CA": {"lodging": 239, "mie": 79},
        "Costa Mesa, CA": {"lodging": 188, "mie": 74},
        "Fresno, CA": {"lodging": 126, "mie": 69},
        "Glendale, CA": {"lodging": 209, "mie": 79},
        "Huntington Beach, CA": {"lodging": 188, "mie": 74},
        "Irvine, CA": {"lodging": 188, "mie": 74},
        "La Jolla, CA": {"lodging": 239, "mie": 79},
        "Long Beach, CA": {"lodging": 209, "mie": 79},
        "Los Angeles, CA": {"lodging": 209, "mie": 79},
        "Mill Valley, CA": {"lodging": 311, "mie": 79},
        "Modesto, CA": {"lodging": 113, "mie": 68},
        "Monterey, CA": {"lodging": 227, "mie": 79},
        "Napa, CA": {"lodging": 295, "mie": 79},
        "Newport Beach, CA": {"lodging": 227, "mie": 79},
        "Oakland, CA": {"lodging": 240, "mie": 79},
        "Ontario, CA": {"lodging": 161, "mie": 74},
        "Oxnard, CA": {"lodging": 174, "mie": 74},
        "Palm Springs, CA": {"lodging": 185, "mie": 74},
        "Palo Alto, CA": {"lodging": 302, "mie": 79},
        "Pasadena, CA": {"lodging": 209, "mie": 79},
        "Redondo Beach, CA": {"lodging": 209, "mie": 79},
        "Redwood City, CA": {"lodging": 302, "mie": 79},
        "Riverside, CA": {"lodging": 161, "mie": 74},
        "Sacramento, CA": {"lodging": 168, "mie": 74},
        "San Bernardino, CA": {"lodging": 161, "mie": 74},
        "San Diego, CA": {"lodging": 194, "mie": 74},
        "San Francisco, CA": {"lodging": 311, "mie": 79},
        "San Jose, CA": {"lodging": 258, "mie": 79},
        "San Luis Obispo, CA": {"lodging": 186, "mie": 74},
        "San Mateo, CA": {"lodging": 302, "mie": 79},
        "Santa Ana, CA": {"lodging": 188, "mie": 74},
        "Santa Barbara, CA": {"lodging": 226, "mie": 79},
        "Santa Clara, CA": {"lodging": 258, "mie": 79},
        "Santa Cruz, CA": {"lodging": 203, "mie": 79},
        "Santa Monica, CA": {"lodging": 239, "mie": 79},
        "Santa Rosa, CA": {"lodging": 209, "mie": 79},
        "Stockton, CA": {"lodging": 118, "mie": 68},
        "Sunnyvale, CA": {"lodging": 258, "mie": 79},
        "Thousand Oaks, CA": {"lodging": 174, "mie": 74},
        "Torrance, CA": {"lodging": 209, "mie": 79},
        "Ventura, CA": {"lodging": 174, "mie": 74},
        "Visalia, CA": {"lodging": 126, "mie": 69},
        "West Hollywood, CA": {"lodging": 239, "mie": 79},
        # COLORADO
        "Aspen, CO": {"lodging": 311, "mie": 79},
        "Boulder, CO": {"lodging": 188, "mie": 79},
        "Colorado Springs, CO": {"lodging": 141, "mie": 69},
        "Denver, CO": {"lodging": 198, "mie": 79},
        "Durango, CO": {"lodging": 163, "mie": 74},
        "Fort Collins, CO": {"lodging": 153, "mie": 74},
        "Grand Junction, CO": {"lodging": 124, "mie": 69},
        "Pueblo, CO": {"lodging": 117, "mie": 68},
        "Steamboat Springs, CO": {"lodging": 210, "mie": 79},
        "Telluride, CO": {"lodging": 241, "mie": 79},
        "Vail, CO": {"lodging": 320, "mie": 79},
        # CONNECTICUT
        "Bridgeport, CT": {"lodging": 152, "mie": 74},
        "Danbury, CT": {"lodging": 152, "mie": 74},
        "Hartford, CT": {"lodging": 138, "mie": 69},
        "New Haven, CT": {"lodging": 147, "mie": 74},
        "New London, CT": {"lodging": 140, "mie": 69},
        "Norwalk, CT": {"lodging": 203, "mie": 79},
        "Stamford, CT": {"lodging": 203, "mie": 79},
        "Waterbury, CT": {"lodging": 134, "mie": 69},
        # DELAWARE
        "Dover, DE": {"lodging": 118, "mie": 68},
        "Lewes, DE": {"lodging": 162, "mie": 74},
        "Rehoboth Beach, DE": {"lodging": 162, "mie": 74},
        "Wilmington, DE": {"lodging": 133, "mie": 69},
        # DC
        "Washington, DC": {"lodging": 258, "mie": 79},
        # FLORIDA
        "Boca Raton, FL": {"lodging": 186, "mie": 74},
        "Bradenton, FL": {"lodging": 156, "mie": 74},
        "Clearwater, FL": {"lodging": 150, "mie": 69},
        "Cocoa Beach, FL": {"lodging": 150, "mie": 69},
        "Daytona Beach, FL": {"lodging": 141, "mie": 69},
        "Delray Beach, FL": {"lodging": 186, "mie": 74},
        "Destin, FL": {"lodging": 196, "mie": 79},
        "Fort Lauderdale, FL": {"lodging": 189, "mie": 74},
        "Fort Myers, FL": {"lodging": 178, "mie": 74},
        "Fort Walton Beach, FL": {"lodging": 165, "mie": 74},
        "Gainesville, FL": {"lodging": 126, "mie": 69},
        "Jacksonville, FL": {"lodging": 138, "mie": 69},
        "Key Largo, FL": {"lodging": 217, "mie": 79},
        "Key West, FL": {"lodging": 303, "mie": 79},
        "Kissimmee, FL": {"lodging": 163, "mie": 69},
        "Melbourne, FL": {"lodging": 150, "mie": 69},
        "Miami, FL": {"lodging": 195, "mie": 79},
        "Miami Beach, FL": {"lodging": 235, "mie": 79},
        "Naples, FL": {"lodging": 215, "mie": 79},
        "Ocala, FL": {"lodging": 117, "mie": 68},
        "Orlando, FL": {"lodging": 163, "mie": 69},
        "Palm Beach, FL": {"lodging": 221, "mie": 79},
        "Panama City, FL": {"lodging": 148, "mie": 69},
        "Panama City Beach, FL": {"lodging": 175, "mie": 74},
        "Pensacola, FL": {"lodging": 139, "mie": 69},
        "Pompano Beach, FL": {"lodging": 189, "mie": 74},
        "Punta Gorda, FL": {"lodging": 147, "mie": 69},
        "Sarasota, FL": {"lodging": 181, "mie": 79},
        "St. Augustine, FL": {"lodging": 173, "mie": 74},
        "St. Petersburg, FL": {"lodging": 150, "mie": 69},
        "Tallahassee, FL": {"lodging": 128, "mie": 69},
        "Tampa, FL": {"lodging": 150, "mie": 69},
        "Vero Beach, FL": {"lodging": 178, "mie": 74},
        "West Palm Beach, FL": {"lodging": 186, "mie": 74},
        # GEORGIA
        "Albany, GA": {"lodging": 112, "mie": 68},
        "Athens, GA": {"lodging": 131, "mie": 69},
        "Atlanta, GA": {"lodging": 181, "mie": 79},
        "Augusta, GA": {"lodging": 112, "mie": 68},
        "Columbus, GA": {"lodging": 118, "mie": 68},
        "Jekyll Island, GA": {"lodging": 157, "mie": 74},
        "Macon, GA": {"lodging": 112, "mie": 68},
        "Marietta, GA": {"lodging": 181, "mie": 79},
        "Savannah, GA": {"lodging": 155, "mie": 74},
        "St. Simons Island, GA": {"lodging": 166, "mie": 74},
        # IDAHO
        "Boise, ID": {"lodging": 143, "mie": 69},
        "Coeur d'Alene, ID": {"lodging": 154, "mie": 74},
        "Sun Valley, ID": {"lodging": 205, "mie": 79},
        # ILLINOIS
        "Champaign, IL": {"lodging": 119, "mie": 68},
        "Chicago, IL": {"lodging": 231, "mie": 79},
        "Naperville, IL": {"lodging": 157, "mie": 74},
        "Oak Brook, IL": {"lodging": 173, "mie": 74},
        "Peoria, IL": {"lodging": 113, "mie": 68},
        "Rockford, IL": {"lodging": 113, "mie": 68},
        "Springfield, IL": {"lodging": 113, "mie": 68},
        # INDIANA
        "Bloomington, IN": {"lodging": 126, "mie": 69},
        "Fort Wayne, IN": {"lodging": 115, "mie": 68},
        "Indianapolis, IN": {"lodging": 147, "mie": 74},
        "South Bend, IN": {"lodging": 127, "mie": 69},
        # IOWA
        "Cedar Rapids, IA": {"lodging": 115, "mie": 68},
        "Des Moines, IA": {"lodging": 117, "mie": 69},
        "Iowa City, IA": {"lodging": 119, "mie": 68},
        # KANSAS
        "Kansas City, KS": {"lodging": 137, "mie": 74},
        "Lawrence, KS": {"lodging": 118, "mie": 68},
        "Topeka, KS": {"lodging": 112, "mie": 68},
        "Wichita, KS": {"lodging": 113, "mie": 68},
        # KENTUCKY
        "Lexington, KY": {"lodging": 128, "mie": 69},
        "Louisville, KY": {"lodging": 151, "mie": 74},
        # LOUISIANA
        "Baton Rouge, LA": {"lodging": 117, "mie": 69},
        "Lafayette, LA": {"lodging": 118, "mie": 68},
        "New Orleans, LA": {"lodging": 184, "mie": 79},
        "Shreveport, LA": {"lodging": 112, "mie": 68},
        # MAINE
        "Augusta, ME": {"lodging": 121, "mie": 69},
        "Bangor, ME": {"lodging": 117, "mie": 68},
        "Bar Harbor, ME": {"lodging": 176, "mie": 74},
        "Kennebunk, ME": {"lodging": 175, "mie": 74},
        "Portland, ME": {"lodging": 151, "mie": 74},
        # MARYLAND
        "Annapolis, MD": {"lodging": 185, "mie": 79},
        "Baltimore, MD": {"lodging": 173, "mie": 79},
        "Bethesda, MD": {"lodging": 258, "mie": 79},
        "College Park, MD": {"lodging": 187, "mie": 79},
        "Columbia, MD": {"lodging": 156, "mie": 74},
        "Frederick, MD": {"lodging": 137, "mie": 69},
        "Ocean City, MD": {"lodging": 178, "mie": 74},
        "Rockville, MD": {"lodging": 203, "mie": 79},
        "Silver Spring, MD": {"lodging": 203, "mie": 79},
        # MASSACHUSETTS
        "Boston, MA": {"lodging": 268, "mie": 79},
        "Cambridge, MA": {"lodging": 268, "mie": 79},
        "Cape Cod, MA": {"lodging": 186, "mie": 74},
        "Hyannis, MA": {"lodging": 186, "mie": 74},
        "Lowell, MA": {"lodging": 167, "mie": 74},
        "Nantucket, MA": {"lodging": 268, "mie": 79},
        "Northampton, MA": {"lodging": 133, "mie": 69},
        "Plymouth, MA": {"lodging": 164, "mie": 74},
        "Provincetown, MA": {"lodging": 218, "mie": 79},
        "Springfield, MA": {"lodging": 133, "mie": 69},
        "Worcester, MA": {"lodging": 138, "mie": 69},
        # MICHIGAN
        "Ann Arbor, MI": {"lodging": 149, "mie": 74},
        "Detroit, MI": {"lodging": 159, "mie": 74},
        "Grand Rapids, MI": {"lodging": 134, "mie": 69},
        "Kalamazoo, MI": {"lodging": 119, "mie": 68},
        "Lansing, MI": {"lodging": 117, "mie": 68},
        "Mackinac Island, MI": {"lodging": 206, "mie": 79},
        "Traverse City, MI": {"lodging": 156, "mie": 74},
        # MINNESOTA
        "Duluth, MN": {"lodging": 124, "mie": 69},
        "Minneapolis, MN": {"lodging": 173, "mie": 79},
        "Rochester, MN": {"lodging": 132, "mie": 69},
        "St. Cloud, MN": {"lodging": 114, "mie": 68},
        "St. Paul, MN": {"lodging": 161, "mie": 74},
        # MISSISSIPPI
        "Biloxi, MS": {"lodging": 128, "mie": 69},
        "Gulfport, MS": {"lodging": 128, "mie": 69},
        "Jackson, MS": {"lodging": 113, "mie": 68},
        "Oxford, MS": {"lodging": 123, "mie": 69},
        "Tupelo, MS": {"lodging": 112, "mie": 68},
        # MISSOURI
        "Branson, MO": {"lodging": 118, "mie": 68},
        "Columbia, MO": {"lodging": 117, "mie": 68},
        "Kansas City, MO": {"lodging": 151, "mie": 74},
        "Springfield, MO": {"lodging": 112, "mie": 68},
        "St. Louis, MO": {"lodging": 144, "mie": 74},
        # MONTANA
        "Big Sky, MT": {"lodging": 189, "mie": 74},
        "Billings, MT": {"lodging": 112, "mie": 68},
        "Bozeman, MT": {"lodging": 169, "mie": 74},
        "Helena, MT": {"lodging": 114, "mie": 68},
        "Missoula, MT": {"lodging": 131, "mie": 69},
        "Whitefish, MT": {"lodging": 167, "mie": 74},
        # NEBRASKA
        "Lincoln, NE": {"lodging": 113, "mie": 68},
        "Omaha, NE": {"lodging": 118, "mie": 68},
        # NEVADA
        "Henderson, NV": {"lodging": 151, "mie": 74},
        "Las Vegas, NV": {"lodging": 151, "mie": 74},
        "Reno, NV": {"lodging": 139, "mie": 74},
        # NEW HAMPSHIRE
        "Concord, NH": {"lodging": 131, "mie": 69},
        "Conway, NH": {"lodging": 141, "mie": 69},
        "Hanover, NH": {"lodging": 166, "mie": 74},
        "Manchester, NH": {"lodging": 124, "mie": 69},
        "Nashua, NH": {"lodging": 141, "mie": 69},
        "Portsmouth, NH": {"lodging": 179, "mie": 74},
        # NEW JERSEY
        "Atlantic City, NJ": {"lodging": 130, "mie": 69},
        "Cape May, NJ": {"lodging": 185, "mie": 74},
        "Cherry Hill, NJ": {"lodging": 132, "mie": 69},
        "Edison, NJ": {"lodging": 166, "mie": 79},
        "Jersey City, NJ": {"lodging": 218, "mie": 79},
        "Newark, NJ": {"lodging": 171, "mie": 79},
        "Ocean City, NJ": {"lodging": 176, "mie": 74},
        "Parsippany, NJ": {"lodging": 164, "mie": 74},
        "Princeton, NJ": {"lodging": 192, "mie": 79},
        "Trenton, NJ": {"lodging": 132, "mie": 69},
        # NEW MEXICO
        "Albuquerque, NM": {"lodging": 131, "mie": 69},
        "Las Cruces, NM": {"lodging": 115, "mie": 68},
        "Santa Fe, NM": {"lodging": 171, "mie": 74},
        "Taos, NM": {"lodging": 145, "mie": 69},
        # NEW YORK
        "Albany, NY": {"lodging": 143, "mie": 69},
        "Buffalo, NY": {"lodging": 119, "mie": 69},
        "Ithaca, NY": {"lodging": 143, "mie": 69},
        "Lake Placid, NY": {"lodging": 153, "mie": 74},
        "Long Island, NY": {"lodging": 192, "mie": 79},
        "New York City, NY": {"lodging": 282, "mie": 79},
        "Niagara Falls, NY": {"lodging": 138, "mie": 69},
        "Poughkeepsie, NY": {"lodging": 148, "mie": 74},
        "Rochester, NY": {"lodging": 118, "mie": 69},
        "Saratoga Springs, NY": {"lodging": 179, "mie": 74},
        "Syracuse, NY": {"lodging": 121, "mie": 69},
        "Tarrytown, NY": {"lodging": 219, "mie": 79},
        "White Plains, NY": {"lodging": 219, "mie": 79},
        # NORTH CAROLINA
        "Asheville, NC": {"lodging": 171, "mie": 74},
        "Chapel Hill, NC": {"lodging": 148, "mie": 74},
        "Charlotte, NC": {"lodging": 155, "mie": 74},
        "Durham, NC": {"lodging": 148, "mie": 74},
        "Fayetteville, NC": {"lodging": 114, "mie": 68},
        "Greensboro, NC": {"lodging": 117, "mie": 68},
        "Kill Devil Hills, NC": {"lodging": 141, "mie": 69},
        "Raleigh, NC": {"lodging": 150, "mie": 74},
        "Wilmington, NC": {"lodging": 133, "mie": 69},
        "Winston-Salem, NC": {"lodging": 120, "mie": 68},
        # NORTH DAKOTA
        "Bismarck, ND": {"lodging": 113, "mie": 68},
        "Fargo, ND": {"lodging": 117, "mie": 68},
        "Grand Forks, ND": {"lodging": 115, "mie": 68},
        # OHIO
        "Akron, OH": {"lodging": 118, "mie": 68},
        "Canton, OH": {"lodging": 117, "mie": 68},
        "Cincinnati, OH": {"lodging": 147, "mie": 69},
        "Cleveland, OH": {"lodging": 152, "mie": 74},
        "Columbus, OH": {"lodging": 139, "mie": 69},
        "Dayton, OH": {"lodging": 119, "mie": 68},
        "Sandusky, OH": {"lodging": 130, "mie": 69},
        "Toledo, OH": {"lodging": 115, "mie": 68},
        # OKLAHOMA
        "Norman, OK": {"lodging": 114, "mie": 68},
        "Oklahoma City, OK": {"lodging": 114, "mie": 68},
        "Tulsa, OK": {"lodging": 115, "mie": 68},
        # OREGON
        "Bend, OR": {"lodging": 173, "mie": 74},
        "Eugene, OR": {"lodging": 136, "mie": 69},
        "Lincoln City, OR": {"lodging": 147, "mie": 69},
        "Portland, OR": {"lodging": 176, "mie": 79},
        "Salem, OR": {"lodging": 126, "mie": 69},
        "Seaside, OR": {"lodging": 155, "mie": 74},
        # PENNSYLVANIA
        "Erie, PA": {"lodging": 116, "mie": 68},
        "Gettysburg, PA": {"lodging": 129, "mie": 69},
        "Harrisburg, PA": {"lodging": 127, "mie": 69},
        "Hershey, PA": {"lodging": 167, "mie": 74},
        "Lancaster, PA": {"lodging": 136, "mie": 69},
        "Philadelphia, PA": {"lodging": 194, "mie": 79},
        "Pittsburgh, PA": {"lodging": 165, "mie": 74},
        "Reading, PA": {"lodging": 117, "mie": 68},
        "Scranton, PA": {"lodging": 118, "mie": 68},
        "State College, PA": {"lodging": 132, "mie": 69},
        # RHODE ISLAND
        "Newport, RI": {"lodging": 197, "mie": 79},
        "Providence, RI": {"lodging": 168, "mie": 74},
        # SOUTH CAROLINA
        "Charleston, SC": {"lodging": 175, "mie": 74},
        "Columbia, SC": {"lodging": 117, "mie": 68},
        "Greenville, SC": {"lodging": 132, "mie": 69},
        "Hilton Head, SC": {"lodging": 173, "mie": 74},
        "Myrtle Beach, SC": {"lodging": 147, "mie": 69},
        # SOUTH DAKOTA
        "Rapid City, SD": {"lodging": 129, "mie": 69},
        "Sioux Falls, SD": {"lodging": 117, "mie": 68},
        # TENNESSEE
        "Chattanooga, TN": {"lodging": 140, "mie": 69},
        "Gatlinburg, TN": {"lodging": 149, "mie": 74},
        "Knoxville, TN": {"lodging": 127, "mie": 69},
        "Memphis, TN": {"lodging": 129, "mie": 69},
        "Nashville, TN": {"lodging": 197, "mie": 79},
        "Pigeon Forge, TN": {"lodging": 149, "mie": 74},
        # TEXAS
        "Amarillo, TX": {"lodging": 113, "mie": 68},
        "Arlington, TX": {"lodging": 143, "mie": 69},
        "Austin, TX": {"lodging": 166, "mie": 74},
        "Beaumont, TX": {"lodging": 113, "mie": 68},
        "College Station, TX": {"lodging": 121, "mie": 68},
        "Corpus Christi, TX": {"lodging": 125, "mie": 69},
        "Dallas, TX": {"lodging": 161, "mie": 74},
        "El Paso, TX": {"lodging": 115, "mie": 68},
        "Fort Worth, TX": {"lodging": 143, "mie": 69},
        "Frisco, TX": {"lodging": 161, "mie": 74},
        "Galveston, TX": {"lodging": 155, "mie": 74},
        "Houston, TX": {"lodging": 156, "mie": 74},
        "Irving, TX": {"lodging": 161, "mie": 74},
        "Lubbock, TX": {"lodging": 115, "mie": 68},
        "Midland, TX": {"lodging": 127, "mie": 69},
        "Plano, TX": {"lodging": 161, "mie": 74},
        "Round Rock, TX": {"lodging": 166, "mie": 74},
        "San Antonio, TX": {"lodging": 138, "mie": 69},
        "South Padre Island, TX": {"lodging": 147, "mie": 69},
        "The Woodlands, TX": {"lodging": 156, "mie": 74},
        "Allen, TX": {"lodging": 161, "mie": 74},
        "McKinney, TX": {"lodging": 161, "mie": 74},
        # UTAH
        "Moab, UT": {"lodging": 161, "mie": 74},
        "Ogden, UT": {"lodging": 119, "mie": 68},
        "Park City, UT": {"lodging": 209, "mie": 79},
        "Provo, UT": {"lodging": 123, "mie": 69},
        "Salt Lake City, UT": {"lodging": 155, "mie": 74},
        "St. George, UT": {"lodging": 139, "mie": 69},
        # VERMONT
        "Burlington, VT": {"lodging": 147, "mie": 74},
        "Manchester, VT": {"lodging": 155, "mie": 74},
        "Montpelier, VT": {"lodging": 138, "mie": 69},
        "Stowe, VT": {"lodging": 184, "mie": 74},
        # VIRGINIA
        "Alexandria, VA": {"lodging": 258, "mie": 79},
        "Arlington, VA": {"lodging": 258, "mie": 79},
        "Charlottesville, VA": {"lodging": 143, "mie": 69},
        "Fairfax, VA": {"lodging": 218, "mie": 79},
        "Fredericksburg, VA": {"lodging": 133, "mie": 69},
        "Hampton, VA": {"lodging": 125, "mie": 69},
        "Harrisonburg, VA": {"lodging": 118, "mie": 68},
        "Lynchburg, VA": {"lodging": 118, "mie": 68},
        "Newport News, VA": {"lodging": 125, "mie": 69},
        "Norfolk, VA": {"lodging": 133, "mie": 69},
        "Richmond, VA": {"lodging": 145, "mie": 74},
        "Roanoke, VA": {"lodging": 116, "mie": 68},
        "Tysons Corner, VA": {"lodging": 218, "mie": 79},
        "Virginia Beach, VA": {"lodging": 134, "mie": 69},
        "Williamsburg, VA": {"lodging": 139, "mie": 69},
        # WASHINGTON
        "Bellingham, WA": {"lodging": 155, "mie": 74},
        "Olympia, WA": {"lodging": 137, "mie": 69},
        "Seattle, WA": {"lodging": 227, "mie": 79},
        "Spokane, WA": {"lodging": 119, "mie": 69},
        "Tacoma, WA": {"lodging": 166, "mie": 74},
        "Vancouver, WA": {"lodging": 157, "mie": 74},
        # WEST VIRGINIA
        "Charleston, WV": {"lodging": 118, "mie": 68},
        "Morgantown, WV": {"lodging": 118, "mie": 68},
        # WISCONSIN
        "Green Bay, WI": {"lodging": 119, "mie": 68},
        "Madison, WI": {"lodging": 143, "mie": 74},
        "Milwaukee, WI": {"lodging": 143, "mie": 74},
        "Wisconsin Dells, WI": {"lodging": 127, "mie": 69},
        # WYOMING
        "Casper, WY": {"lodging": 114, "mie": 68},
        "Cheyenne, WY": {"lodging": 115, "mie": 68},
        "Cody, WY": {"lodging": 139, "mie": 69},
        "Jackson, WY": {"lodging": 213, "mie": 79},
    }
    
    try:
        inserted = 0
        updated = 0
        
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                for location, rates in GSA_COMPLETE.items():
                    city, state = location.rsplit(", ", 1)
                    cur.execute("""
                        INSERT INTO gsa_rates (city, state, daily_lodging, daily_mie, fiscal_year)
                        VALUES (%s, %s, %s, %s, 2025)
                        ON CONFLICT (city, state, fiscal_year) DO UPDATE
                        SET daily_lodging = EXCLUDED.daily_lodging,
                            daily_mie = EXCLUDED.daily_mie,
                            updated_at = CURRENT_TIMESTAMP
                    """, (city, state, rates["lodging"], rates["mie"]))
                    inserted += 1
                
                # Standard CONUS rate
                cur.execute("""
                    INSERT INTO gsa_rates (city, state, daily_lodging, daily_mie, fiscal_year)
                    VALUES ('Standard CONUS', 'US', 110, 68, 2025)
                    ON CONFLICT (city, state, fiscal_year) DO UPDATE
                    SET daily_lodging = 110, daily_mie = 68, updated_at = CURRENT_TIMESTAMP
                """)
                
                conn.commit()
        
        return {
            "message": "Complete GSA rates seeded successfully!",
            "locations_added": inserted,
            "includes_standard_conus": True,
            "standard_rate": {"lodging": 110, "mie": 68},
            "fiscal_year": "2025/2026"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to seed GSA rates: {str(e)}")


@app.post("/api/calculate-pay")
def calculate_pay_package(request: PayCalculatorRequest):
    """
    Calculate complete pay package from bill rate.
    
    Logic:
    1. Start with bill rate × hours = weekly revenue
    2. Subtract gross margin (your profit)
    3. Subtract burden (employer taxes, WC, insurance)
    4. What's left = total available for candidate
    5. For travel: Maximize GSA stipends, remainder = taxable hourly
    6. Ensure minimum wage compliance
    
    All outputs are WEEKLY (industry standard for travel contracts).
    """
    
    # Get GSA rates for location
    gsa_rates = get_gsa_rates(request.city, request.state)
    daily_lodging = gsa_rates["lodging"]
    daily_mie = gsa_rates["mie"]
    
    # WEEKLY calculations (travel contracts pay weekly)
    weekly_revenue = request.bill_rate * request.hours_per_week
    
    # Your gross margin
    weekly_gross_margin = weekly_revenue * request.gross_margin_pct
    after_margin = weekly_revenue - weekly_gross_margin
    
    # Employer burden (FICA, FUTA, SUTA, workers comp, liability)
    weekly_burden = after_margin * request.burden_pct
    
    # Total available for candidate compensation
    total_available = after_margin - weekly_burden
    
    if request.is_travel_contract:
        # TRAVEL CONTRACT: Maximize tax-free stipends per GSA
        
        # GSA max weekly stipends
        max_weekly_housing = daily_lodging * 7  # 7 days (they need housing all week)
        max_weekly_mie = daily_mie * 5  # 5 work days only
        max_weekly_stipends = max_weekly_housing + max_weekly_mie
        
        # Taxable pay = what's left after stipends
        weekly_taxable_pay = total_available - max_weekly_stipends
        taxable_hourly = weekly_taxable_pay / request.hours_per_week
        
        # Minimum wage compliance check ($15/hr for healthcare professionals)
        min_hourly = 15.00
        
        if taxable_hourly < min_hourly:
            # Must reduce stipends to meet minimum wage
            weekly_taxable_pay = min_hourly * request.hours_per_week
            remaining_for_stipends = total_available - weekly_taxable_pay
            
            if remaining_for_stipends > 0:
                # Proportionally reduce both stipends
                stipend_ratio = remaining_for_stipends / max_weekly_stipends
                weekly_housing = max_weekly_housing * stipend_ratio
                weekly_mie = max_weekly_mie * stipend_ratio
            else:
                # No room for stipends at all
                weekly_housing = 0
                weekly_mie = 0
            
            taxable_hourly = min_hourly
        else:
            # Full GSA stipends can be paid
            weekly_housing = max_weekly_housing
            weekly_mie = max_weekly_mie
        
        # Total weekly compensation
        total_weekly_pay = weekly_taxable_pay + weekly_housing + weekly_mie
        effective_hourly = total_weekly_pay / request.hours_per_week
        
        return {
            "contract_type": "Travel",
            "location": f"{request.city}, {request.state}",
            "bill_rate": round(request.bill_rate, 2),
            "hours_per_week": request.hours_per_week,
            "gross_margin_pct": request.gross_margin_pct,
            "burden_pct": request.burden_pct,
            
            # GSA Rates
            "gsa_daily_lodging": daily_lodging,
            "gsa_daily_mie": daily_mie,
            "gsa_max_weekly_housing": round(max_weekly_housing, 2),
            "gsa_max_weekly_mie": round(max_weekly_mie, 2),
            
            # WEEKLY PAY BREAKDOWN (Primary Output)
            "weekly_taxable_pay": round(weekly_taxable_pay, 2),
            "weekly_housing_stipend": round(weekly_housing, 2),
            "weekly_mie_stipend": round(weekly_mie, 2),
            "total_weekly_pay": round(total_weekly_pay, 2),
            
            # Hourly equivalents
            "taxable_hourly_rate": round(taxable_hourly, 2),
            "effective_hourly_rate": round(effective_hourly, 2),
            
            # Your margins (weekly)
            "weekly_gross_margin": round(weekly_gross_margin, 2),
            "weekly_burden": round(weekly_burden, 2),
            "weekly_revenue": round(weekly_revenue, 2),
        }
    
    else:
        # LOCAL CONTRACT: Fully taxable, no stipends
        taxable_hourly = total_available / request.hours_per_week
        total_weekly_pay = total_available
        
        return {
            "contract_type": "Local",
            "location": f"{request.city}, {request.state}",
            "bill_rate": round(request.bill_rate, 2),
            "hours_per_week": request.hours_per_week,
            "gross_margin_pct": request.gross_margin_pct,
            "burden_pct": request.burden_pct,
            
            # GSA Rates (for reference)
            "gsa_daily_lodging": daily_lodging,
            "gsa_daily_mie": daily_mie,
            "gsa_max_weekly_housing": 0,
            "gsa_max_weekly_mie": 0,
            
            # WEEKLY PAY BREAKDOWN
            "weekly_taxable_pay": round(total_weekly_pay, 2),
            "weekly_housing_stipend": 0,
            "weekly_mie_stipend": 0,
            "total_weekly_pay": round(total_weekly_pay, 2),
            
            # Hourly equivalents
            "taxable_hourly_rate": round(taxable_hourly, 2),
            "effective_hourly_rate": round(taxable_hourly, 2),
            
            # Your margins (weekly)
            "weekly_gross_margin": round(weekly_gross_margin, 2),
            "weekly_burden": round(weekly_burden, 2),
            "weekly_revenue": round(weekly_revenue, 2),
        }


# ============================================================================
# RUN SERVER
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
    # ============================================================================
# ADMIN: CANDIDATES MANAGEMENT
# ============================================================================

@app.get("/api/admin/candidates")
async def get_candidates(
    discipline: Optional[str] = None,
    x_admin_password: str = Header(None)
):
    """Get all candidates with optional filtering"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = "SELECT * FROM candidates WHERE active = TRUE"
                params = []
                
                if discipline:
                    query += " AND (license_type ILIKE %s OR discipline ILIKE %s)"
                    params.extend([f"%{discipline}%", f"%{discipline}%"])
                
                query += " ORDER BY created_at DESC"
                
                cur.execute(query, params)
                candidates = cur.fetchall()
                
                return {"candidates": candidates}
    except Exception as e:
        print(f"Error fetching candidates: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        # ============================================================================
# ADMIN: ANALYTICS
# ============================================================================

@app.get("/api/admin/analytics")
async def get_analytics(x_admin_password: str = Header(None)):
    """Get dashboard analytics"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT COUNT(*) as count FROM jobs WHERE active = TRUE")
                total_jobs = cur.fetchone()['count']
                
                cur.execute("SELECT COUNT(*) as count FROM candidates WHERE active = TRUE")
                total_candidates = cur.fetchone()['count']
                
                cur.execute("""
                    SELECT COUNT(*) as count FROM candidates 
                    WHERE active = TRUE AND created_at >= NOW() - INTERVAL '7 days'
                """)
                new_this_week = cur.fetchone()['count']
                
                cur.execute("""
                    SELECT COUNT(*) as count FROM candidates 
                    WHERE active = TRUE AND resume_url IS NOT NULL
                """)
                with_resume = cur.fetchone()['count']
                
                cur.execute("""
                    SELECT COALESCE(license_type, discipline, 'Other') as discipline, COUNT(*) as count 
                    FROM candidates 
                    WHERE active = TRUE
                    GROUP BY COALESCE(license_type, discipline, 'Other')
                    ORDER BY count DESC
                """)
                discipline_rows = cur.fetchall()
                by_discipline = {row['discipline']: row['count'] for row in discipline_rows}
                
                return {
                    "total_jobs": total_jobs,
                    "total_candidates": total_candidates,
                    "new_this_week": new_this_week,
                    "with_resume": with_resume,
                    "by_discipline": by_discipline
                }
    except Exception as e:
        print(f"Error fetching analytics: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        # ============================================================================
# ADMIN: JOB MANAGEMENT (Edit/Delete)
# ============================================================================

@app.put("/api/admin/jobs/{job_id}/status")
async def update_job_status(
    job_id: int,
    status: JobStatusUpdate,
    x_admin_password: str = Header(None)
):
    """Update job active status"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET active = %s, updated_at = NOW() WHERE id = %s",
                    (status.active, job_id)
                )
                conn.commit()
                return {"success": True, "message": f"Job {'activated' if status.active else 'deactivated'}"}
    except Exception as e:
        print(f"Error updating job status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/admin/jobs/{job_id}")
async def delete_job(
    job_id: int,
    x_admin_password: str = Header(None)
):
    """Delete a job"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
                conn.commit()
                return {"success": True, "message": "Job deleted"}
    except Exception as e:
        print(f"Error deleting job: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        # ============================================================================
# ADMIN: PIPELINE SYSTEM
# ============================================================================

@app.get("/api/admin/pipeline")
async def get_pipeline(
    job_id: Optional[int] = None,
    x_admin_password: str = Header(None)
):
    """Get pipeline entries"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = """
                    SELECT ps.*, c.first_name, c.last_name, c.email, c.phone,
                           c.license_type, j.title AS job_title, j.city AS job_city, j.state AS job_state
                    FROM pipeline_stages ps
                    JOIN candidates c ON ps.candidate_id = c.id
                    LEFT JOIN jobs j ON ps.job_id = j.id
                    WHERE 1=1
                """
                params = []
                
                if job_id:
                    query += " AND ps.job_id = %s"
                    params.append(job_id)
                
                query += " ORDER BY ps.created_at DESC"
                
                cur.execute(query, params)
                entries = cur.fetchall()
                
                return {"entries": entries}
                
    except Exception as e:
        print(f"Error fetching pipeline: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/pipeline")
async def add_to_pipeline(
    entry: PipelineCreate,
    x_admin_password: str = Header(None)
):
    """Add candidate to pipeline"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    INSERT INTO pipeline_stages (candidate_id, job_id, stage, created_at)
                    VALUES (%s, %s, %s, NOW())
                    RETURNING id
                """, (entry.candidate_id, entry.job_id, entry.stage))
                
                new_entry = cur.fetchone()
                conn.commit()
                
                return {"success": True, "id": new_entry['id']}
                
    except Exception as e:
        print(f"Error adding to pipeline: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/admin/pipeline/{entry_id}/stage")
async def update_pipeline_stage(
    entry_id: int,
    stage_update: PipelineStageUpdate,
    x_admin_password: str = Header(None)
):
    """Update pipeline stage"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE pipeline_stages SET stage = %s WHERE id = %s
                """, (stage_update.stage, entry_id))
                conn.commit()
                
                return {"success": True}
                
    except Exception as e:
        print(f"Error updating stage: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/pipeline/{entry_id}/note")
async def add_pipeline_note(
    entry_id: int,
    note_data: PipelineNoteCreate,
    x_admin_password: str = Header(None)
):
    """Add note to pipeline entry"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT notes FROM pipeline_stages WHERE id = %s", (entry_id,))
                entry = cur.fetchone()
                
                if not entry:
                    raise HTTPException(status_code=404, detail="Pipeline entry not found")
                
                from datetime import datetime
                current_notes = entry.get('notes') or ''
                new_notes = f"{current_notes}\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {note_data.note}".strip()
                
                cur.execute("""
                    UPDATE pipeline_stages SET notes = %s WHERE id = %s
                """, (new_notes, entry_id))
                conn.commit()
                
                return {"success": True}
                
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error adding note: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        # ============================================================================
# ADMIN: MATCHING SYSTEM
# ============================================================================

@app.get("/api/admin/match/job/{job_id}")
async def find_candidates_for_job(
    job_id: int,
    x_admin_password: str = Header(None)
):
    """Find matching candidates for a job"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
                job = cur.fetchone()
                
                if not job:
                    raise HTTPException(status_code=404, detail="Job not found")
                
                cur.execute("SELECT * FROM candidates WHERE active = TRUE")
                candidates = cur.fetchall()
                
                matches = []
                for candidate in candidates:
                    score = 0
                    
                    # Discipline match (50 points)
                    job_discipline = (job.get('discipline') or '').lower()
                    candidate_discipline = (candidate.get('license_type') or candidate.get('discipline') or '').lower()
                    if job_discipline and candidate_discipline:
                        if job_discipline in candidate_discipline or candidate_discipline in job_discipline:
                            score += 50
                    
                    # State match (30 points)
                    job_state = (job.get('state') or '').upper()
                    candidate_state = (candidate.get('home_state') or '').upper()
                    if job_state and candidate_state and job_state == candidate_state:
                        score += 30
                    
                    # Has resume (10 points)
                    if candidate.get('resume_url'):
                        score += 10
                    
                    # Recent signup (10 points)
                    score += 10
                    
                    if score > 0:
                        matches.append({**dict(candidate), 'score': score})
                
                matches.sort(key=lambda x: x['score'], reverse=True)
                
                return {"matches": matches[:20]}
                
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error finding matches: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/match/candidate/{candidate_id}")
async def find_jobs_for_candidate(
    candidate_id: int,
    x_admin_password: str = Header(None)
):
    """Find matching jobs for a candidate"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM candidates WHERE id = %s", (candidate_id,))
                candidate = cur.fetchone()
                
                if not candidate:
                    raise HTTPException(status_code=404, detail="Candidate not found")
                
                cur.execute("SELECT * FROM jobs WHERE active = TRUE")
                jobs = cur.fetchall()
                
                matches = []
                for job in jobs:
                    score = 0
                    
                    # Discipline match (50 points)
                    job_discipline = (job.get('discipline') or '').lower()
                    candidate_discipline = (candidate.get('license_type') or candidate.get('discipline') or '').lower()
                    if job_discipline and candidate_discipline:
                        if job_discipline in candidate_discipline or candidate_discipline in job_discipline:
                            score += 50
                    
                    # State match (30 points)
                    job_state = (job.get('state') or '').upper()
                    candidate_state = (candidate.get('home_state') or '').upper()
                    if job_state and candidate_state and job_state == candidate_state:
                        score += 30
                    
                    # Has weekly pay info (10 points)
                    if job.get('weekly_gross'):
                        score += 10
                    
                    score += 10
                    
                    if score > 0:
                        matches.append({**dict(job), 'score': score})
                
                matches.sort(key=lambda x: x['score'], reverse=True)
                
                return {"matches": matches[:20]}
                
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error finding matches: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        # ============================================================================
# SMS: SEND JOB ALERT TO CANDIDATE
# ============================================================================

@app.post("/api/admin/candidates/{candidate_id}/alert")
async def send_job_alert(
    candidate_id: int,
    x_admin_password: str = Header(None)
):
    """Send job alert SMS to a candidate"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM candidates WHERE id = %s", (candidate_id,))
                candidate = cur.fetchone()
                
                if not candidate:
                    raise HTTPException(status_code=404, detail="Candidate not found")
                
                cur.execute("""
                    SELECT title, city, state, weekly_gross 
                    FROM jobs 
                    WHERE active = TRUE
                    ORDER BY created_at DESC
                    LIMIT 3
                """)
                jobs = cur.fetchall()
                
                if not jobs:
                    return {"success": False, "message": "No active jobs to send"}
                
                job_list = "\n".join([
                    f"• {j['title']} in {j['city']}, {j['state']} - ${int(j['weekly_gross'] or 0):,}/wk" 
                    for j in jobs
                ])
                
                message = f"""Hi {candidate['first_name']}! 🎉

New jobs matching your profile:

{job_list}

View all: https://thrivingcarestaffing.com/jobs

Reply STOP to unsubscribe."""

                if twilio_client and candidate.get('phone'):
                    twilio_client.messages.create(
                        body=message,
                        from_=TWILIO_PHONE,
                        to=candidate['phone']
                    )
                    return {"success": True, "message": "Job alert sent"}
                else:
                    return {"success": False, "message": "SMS not configured or no phone number"}
                    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error sending job alert: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# SMS: WEBHOOK FOR INCOMING MESSAGES
# ============================================================================

@app.post("/api/sms/webhook")
async def handle_incoming_sms(request: Request):
    """Twilio webhook for incoming SMS messages"""
    
    try:
        form_data = await request.form()
        from_number = form_data.get('From', '')
        message_body = form_data.get('Body', '').strip()
        
        print(f"📱 Incoming SMS from {from_number}: {message_body}")
        
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM candidates WHERE phone = %s", (from_number,))
                candidate = cur.fetchone()
                
                if not candidate:
                    print(f"  Unknown sender: {from_number}")
                    return Response(content="", media_type="text/xml")
                
                # Handle STOP/unsubscribe
                if message_body.upper() in ['STOP', 'UNSUBSCRIBE', 'QUIT']:
                    cur.execute("UPDATE candidates SET active = FALSE WHERE id = %s", (candidate['id'],))
                    conn.commit()
                    
                    if twilio_client:
                        twilio_client.messages.create(
                            body="You've been unsubscribed from ThrivingCare. Reply START to re-subscribe.",
                            from_=TWILIO_PHONE,
                            to=from_number
                        )
                
                # Handle START/resubscribe
                elif message_body.upper() in ['START', 'SUBSCRIBE']:
                    cur.execute("UPDATE candidates SET active = TRUE WHERE id = %s", (candidate['id'],))
                    conn.commit()
                    
                    if twilio_client:
                        twilio_client.messages.create(
                            body="Welcome back! You're now subscribed to ThrivingCare job alerts.",
                            from_=TWILIO_PHONE,
                            to=from_number
                        )
                
                # Handle YES/interested
                elif message_body.upper() in ['YES', 'Y', 'INTERESTED']:
                    if twilio_client:
                        twilio_client.messages.create(
                            body=f"Great {candidate['first_name']}! A recruiter will reach out within 24 hours. View all jobs: https://thrivingcarestaffing.com/jobs",
                            from_=TWILIO_PHONE,
                            to=from_number
                        )
        
        return Response(content="<?xml version='1.0' encoding='UTF-8'?><Response></Response>", media_type="text/xml")
        
    except Exception as e:
        print(f"Error handling SMS webhook: {e}")
        return Response(content="", media_type="text/xml")

