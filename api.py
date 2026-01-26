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

from fastapi import FastAPI, File, UploadFile, HTTPException, Form, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from decimal import Decimal
import os
from datetime import datetime
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
    "Miami, FL": {"lodging": 
