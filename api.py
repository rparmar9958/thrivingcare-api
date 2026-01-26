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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
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
# AUTO-MATCHING SYSTEM
# ============================================================================

def auto_match_candidate_to_jobs(candidate_id: int, phone: str, first_name: str, discipline: str):
    """When a new candidate signs up, find matching jobs and notify them"""
    
    if not twilio_client or not phone:
        return
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, title, city, state, weekly_gross, facility
                    FROM jobs 
                    WHERE active = TRUE 
                    AND enriched = TRUE
                    AND discipline ILIKE %s
                    ORDER BY weekly_gross DESC
                    LIMIT 3
                """, (f"%{discipline}%",))
                
                jobs = cur.fetchall()
                
                if not jobs:
                    cur.execute("""
                        SELECT id, title, city, state, weekly_gross, facility
                        FROM jobs 
                        WHERE active = TRUE AND enriched = TRUE
                        ORDER BY created_at DESC
                        LIMIT 3
                    """)
                    jobs = cur.fetchall()
                
                if jobs:
                    job_list = "\n".join([
                        f"• {j['title']} in {j['city']}, {j['state']} - ${int(j['weekly_gross'] or 0):,}/wk"
                        for j in jobs
                    ])
                    
                    match_message = f"""🎯 {first_name}, we found jobs for you!

{job_list}

View all & apply: https://thrivingcarestaffing.com/jobs

Reply INTERESTED to any job title for more details!"""
                    
                    twilio_client.messages.create(
                        body=match_message,
                        from_=TWILIO_PHONE,
                        to=phone
                    )
                    
                    cur.execute("""
                        INSERT INTO activity_log (type, description, candidate_id, created_at)
                        VALUES ('job_match_sent', %s, %s, NOW())
                    """, (f"Sent {len(jobs)} job matches via SMS", candidate_id))
                    conn.commit()
                    
                    print(f"  Sent {len(jobs)} job matches to candidate {candidate_id}")
                    
    except Exception as e:
        print(f"Error in auto_match_candidate_to_jobs: {e}")


def auto_match_job_to_candidates(job_id: int, title: str, discipline: str, city: str, state: str, weekly_gross: float) -> int:
    """When a new job is posted, find matching candidates and notify them"""
    
    if not twilio_client:
        return 0
    
    notified = 0
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, first_name, phone, license_type, home_state
                    FROM candidates 
                    WHERE active = TRUE 
                    AND phone IS NOT NULL
                    AND (
                        license_type ILIKE %s 
                        OR discipline ILIKE %s
                        OR home_state = %s
                    )
                    ORDER BY created_at DESC
                    LIMIT 20
                """, (f"%{discipline}%", f"%{discipline}%", state))
                
                candidates = cur.fetchall()
                
                for candidate in candidates:
                    try:
                        cur.execute("""
                            SELECT id FROM job_notifications 
                            WHERE candidate_id = %s AND job_id = %s
                        """, (candidate['id'], job_id))
                        
                        if cur.fetchone():
                            continue
                        
                        message = f"""🚨 New {discipline} opportunity!

{title}
📍 {city}, {state}
💰 ${int(weekly_gross):,}/week

Interested? Reply YES or view: https://thrivingcarestaffing.com/jobs

- ThrivingCare Team"""
                        
                        twilio_client.messages.create(
                            body=message,
                            from_=TWILIO_PHONE,
                            to=candidate['phone']
                        )
                        
                        cur.execute("""
                            INSERT INTO job_notifications (candidate_id, job_id, sent_at)
                            VALUES (%s, %s, NOW())
                        """, (candidate['id'], job_id))
                        
                        notified += 1
                        
                    except Exception as e:
                        print(f"Failed to notify candidate {candidate['id']}: {e}")
                
                if notified > 0:
                    cur.execute("""
                        INSERT INTO activity_log (type, description, job_id, created_at)
                        VALUES ('job_notifications_sent', %s, %s, NOW())
                    """, (f"Notified {notified} candidates about new job", job_id))
                
                conn.commit()
                
    except Exception as e:
        print(f"Error in auto_match_job_to_candidates: {e}")
    
    return notified
    # ============================================================================
# AUTOMATED VETTING SYSTEM
# ============================================================================

VETTING_QUESTIONS = [
    {
        "id": 1,
        "question": "What state(s) are you licensed in? Please list all (e.g., TX, CA, NY)",
        "field": "license_states",
        "type": "text"
    },
    {
        "id": 2,
        "question": "What is your license number for your primary state?",
        "field": "license_number",
        "type": "text"
    },
    {
        "id": 3,
        "question": "When are you available to start a new contract? (e.g., ASAP, Jan 15, 2 weeks)",
        "field": "available_date",
        "type": "text"
    },
    {
        "id": 4,
        "question": "What is your minimum weekly pay requirement? (just the number, e.g., 1500)",
        "field": "min_weekly_pay",
        "type": "number"
    },
    {
        "id": 5,
        "question": "Are you open to travel/relocation for the right opportunity? (YES/NO)",
        "field": "open_to_travel",
        "type": "boolean"
    }
]


def start_vetting_flow(candidate_id: int, phone: str, first_name: str):
    """Start the automated vetting flow for a new candidate"""
    
    if not twilio_client or not phone:
        return
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO candidate_vetting (candidate_id, current_question, status, started_at)
                    VALUES (%s, 1, 'in_progress', NOW())
                    ON CONFLICT (candidate_id) DO UPDATE SET current_question = 1, status = 'in_progress'
                """, (candidate_id,))
                conn.commit()
        
        import time
        time.sleep(2)
        
        send_vetting_question(candidate_id, phone, first_name, 1)
        
    except Exception as e:
        print(f"Error starting vetting flow: {e}")


def send_vetting_question(candidate_id: int, phone: str, first_name: str, question_num: int):
    """Send a vetting question to the candidate"""
    
    if not twilio_client or question_num > len(VETTING_QUESTIONS):
        return
    
    question = VETTING_QUESTIONS[question_num - 1]
    
    try:
        message = f"""📋 Quick question {question_num}/5, {first_name}:

{question['question']}

(Reply with your answer)"""
        
        twilio_client.messages.create(
            body=message,
            from_=TWILIO_PHONE,
            to=phone
        )
        
        print(f"  Sent vetting Q{question_num} to candidate {candidate_id}")
        
    except Exception as e:
        print(f"Error sending vetting question: {e}")


# ============================================================================
# ENDPOINTS
# ============================================================================

@app.get("/")
def read_root():
    """Health check"""
    return {
        "status": "healthy",
        "service": "ThrivingCare API",
        "version": "2.0",
        "timestamp": datetime.now().isoformat()
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
        
        # Admin job creation columns
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
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS license_states_verified TEXT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS license_number VARCHAR(100)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS available_start_date VARCHAR(100)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS min_weekly_pay DECIMAL(10,2)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS open_to_travel BOOLEAN",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS vetting_complete BOOLEAN DEFAULT FALSE",
        
        # ADMINS TABLE
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
        
        # PIPELINE STAGES TABLE
        """CREATE TABLE IF NOT EXISTS pipeline_stages (
            id SERIAL PRIMARY KEY,
            candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
            job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
            stage VARCHAR(50) NOT NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        
        # ACTIVITY LOG TABLE
        """CREATE TABLE IF NOT EXISTS activity_log (
            id SERIAL PRIMARY KEY,
            type VARCHAR(50) NOT NULL,
            description TEXT,
            candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
            job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        
        # AI VETTING LOGS TABLE
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
        
        # GSA PER DIEM RATES TABLE
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
        
        # CANDIDATE VETTING TABLE
        """CREATE TABLE IF NOT EXISTS candidate_vetting (
            id SERIAL PRIMARY KEY,
            candidate_id INTEGER UNIQUE REFERENCES candidates(id) ON DELETE CASCADE,
            current_question INTEGER DEFAULT 1,
            status VARCHAR(50) DEFAULT 'pending',
            license_states TEXT,
            license_number VARCHAR(100),
            available_date VARCHAR(100),
            min_weekly_pay DECIMAL(10,2),
            open_to_travel BOOLEAN,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        
        # JOB NOTIFICATIONS TABLE
        """CREATE TABLE IF NOT EXISTS job_notifications (
            id SERIAL PRIMARY KEY,
            candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
            job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            response VARCHAR(50),
            responded_at TIMESTAMP,
            UNIQUE(candidate_id, job_id)
        )""",
        
        # SMS MESSAGES LOG TABLE
        """CREATE TABLE IF NOT EXISTS sms_messages (
            id SERIAL PRIMARY KEY,
            candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
            phone VARCHAR(20),
            direction VARCHAR(10),
            message TEXT,
            message_type VARCHAR(50),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        return {"count": count}
    except Exception as e:
        print(f"Error getting jobs count: {e}")
        return {"count": 0}


@app.post("/api/candidates", response_model=CandidateResponse)
async def create_candidate(candidate: CandidateIntake):
    """
    Create new candidate from website signup
    """
    
    try:
        address_parts = parse_address(candidate.homeAddress)
        
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
        
        # AUTO-MATCHING: Find and notify about matching jobs
        try:
            auto_match_candidate_to_jobs(candidate_id, candidate.phone, candidate.firstName, candidate.discipline)
        except Exception as e:
            print(f"Auto-match error: {e}")
        
        # START VETTING: Send first vetting question after a delay
        try:
            start_vetting_flow(candidate_id, candidate.phone, candidate.firstName)
        except Exception as e:
            print(f"Vetting start error: {e}")
        
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
    """Upload resume for candidate"""
    
    try:
        allowed_types = ['application/pdf', 'application/msword', 
                        'application/vnd.openxmlformats-officedocument.wordprocessingml.document']
        
        if resume.content_type not in allowed_types:
            raise HTTPException(status_code=400, detail="Invalid file type. Please upload PDF or Word document.")
        
        resume_content = await resume.read()
        if len(resume_content) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File too large. Maximum size is 5MB.")
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        file_extension = resume.filename.split('.')[-1]
        s3_key = f"resumes/candidate_{candidate_id}_{timestamp}.{file_extension}"
        
        if s3_client:
            s3_client.put_object(
                Bucket=AWS_BUCKET,
                Key=s3_key,
                Body=resume_content,
                ContentType=resume.content_type
            )
            resume_url = f"https://{AWS_BUCKET}.s3.amazonaws.com/{s3_key}"
        else:
            os.makedirs('uploads/resumes', exist_ok=True)
            filepath = f"uploads/resumes/{s3_key}"
            with open(filepath, 'wb') as f:
                f.write(resume_content)
            resume_url = f"/uploads/resumes/{s3_key}"
        
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


# ============================================================================
# TWILIO WEBHOOK - INCOMING SMS
# ============================================================================

@app.post("/api/sms/webhook")
async def handle_incoming_sms(request: Request):
    """Twilio webhook for incoming SMS messages."""
    
    try:
        form_data = await request.form()
        from_number = form_data.get('From', '')
        message_body = form_data.get('Body', '').strip()
        
        print(f"📱 Incoming SMS from {from_number}: {message_body}")
        
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT c.*, cv.current_question, cv.status as vetting_status
                    FROM candidates c
                    LEFT JOIN candidate_vetting cv ON c.id = cv.candidate_id
                    WHERE c.phone = %s
                """, (from_number,))
                
                candidate = cur.fetchone()
                
                if not candidate:
                    print(f"  Unknown sender: {from_number}")
                    return Response(content="", media_type="text/xml")
                
                candidate_id = candidate['id']
                first_name = candidate['first_name']
                
                cur.execute("""
                    INSERT INTO sms_messages (candidate_id, phone, direction, message, message_type, created_at)
                    VALUES (%s, %s, 'inbound', %s, 'response', NOW())
                """, (candidate_id, from_number, message_body))
                conn.commit()
                
                if candidate['vetting_status'] == 'in_progress':
                    response_msg = process_vetting_response(
                        candidate_id, 
                        from_number, 
                        first_name,
                        candidate['current_question'],
                        message_body
                    )
                    
                    if response_msg and twilio_client:
                        twilio_client.messages.create(
                            body=response_msg,
                            from_=TWILIO_PHONE,
                            to=from_number
                        )
                
                elif message_body.upper() in ['YES', 'INTERESTED', 'Y']:
                    handle_job_interest(candidate_id, from_number, first_name)
                
                elif message_body.upper() in ['STOP', 'UNSUBSCRIBE', 'QUIT']:
                    cur.execute("""
                        UPDATE candidates SET active = FALSE WHERE id = %s
                    """, (candidate_id,))
                    conn.commit()
                    
                    if twilio_client:
                        twilio_client.messages.create(
                            body="You've been unsubscribed from ThrivingCare. Reply START to re-subscribe.",
                            from_=TWILIO_PHONE,
                            to=from_number
                        )
                
                cur.execute("""
                    INSERT INTO activity_log (type, description, candidate_id, created_at)
                    VALUES ('sms_received', %s, %s, NOW())
                """, (f"Received: {message_body[:50]}...", candidate_id))
                conn.commit()
        
        return Response(content="<?xml version='1.0' encoding='UTF-8'?><Response></Response>", media_type="text/xml")
        
    except Exception as e:
        print(f"Error handling SMS webhook: {e}")
        return Response(content="", media_type="text/xml")
        def process_vetting_response(candidate_id: int, phone: str, first_name: str, question_num: int, response: str) -> str:
    """Process a vetting question response and send next question"""
    
    if question_num > len(VETTING_QUESTIONS):
        return None
    
    question = VETTING_QUESTIONS[question_num - 1]
    field = question['field']
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if field == 'license_states':
                    states = ','.join([s.strip().upper() for s in response.replace(' ', ',').split(',')])
                    cur.execute("""
                        UPDATE candidate_vetting SET license_states = %s WHERE candidate_id = %s
                    """, (states, candidate_id))
                    cur.execute("""
                        UPDATE candidates SET license_states_verified = %s WHERE id = %s
                    """, (states, candidate_id))
                    
                elif field == 'license_number':
                    cur.execute("""
                        UPDATE candidate_vetting SET license_number = %s WHERE candidate_id = %s
                    """, (response, candidate_id))
                    cur.execute("""
                        UPDATE candidates SET license_number = %s WHERE id = %s
                    """, (response, candidate_id))
                    
                elif field == 'available_date':
                    cur.execute("""
                        UPDATE candidate_vetting SET available_date = %s WHERE candidate_id = %s
                    """, (response, candidate_id))
                    cur.execute("""
                        UPDATE candidates SET available_start_date = %s WHERE id = %s
                    """, (response, candidate_id))
                    
                elif field == 'min_weekly_pay':
                    try:
                        pay = float(''.join(c for c in response if c.isdigit() or c == '.'))
                    except:
                        pay = 0
                    cur.execute("""
                        UPDATE candidate_vetting SET min_weekly_pay = %s WHERE candidate_id = %s
                    """, (pay, candidate_id))
                    cur.execute("""
                        UPDATE candidates SET min_weekly_pay = %s WHERE id = %s
                    """, (pay, candidate_id))
                    
                elif field == 'open_to_travel':
                    is_open = response.upper() in ['YES', 'Y', 'YEP', 'YEAH', 'SURE', 'OK']
                    cur.execute("""
                        UPDATE candidate_vetting SET open_to_travel = %s WHERE candidate_id = %s
                    """, (is_open, candidate_id))
                    cur.execute("""
                        UPDATE candidates SET open_to_travel = %s WHERE id = %s
                    """, (is_open, candidate_id))
                
                cur.execute("""
                    INSERT INTO ai_vetting_logs (candidate_id, question, response, question_type, created_at)
                    VALUES (%s, %s, %s, 'automated', NOW())
                """, (candidate_id, question['question'], response))
                
                next_question = question_num + 1
                
                if next_question > len(VETTING_QUESTIONS):
                    cur.execute("""
                        UPDATE candidate_vetting SET status = 'complete', completed_at = NOW(), current_question = %s
                        WHERE candidate_id = %s
                    """, (next_question, candidate_id))
                    cur.execute("""
                        UPDATE candidates SET vetting_complete = TRUE, ai_vetting_status = 'complete' WHERE id = %s
                    """, (candidate_id,))
                    
                    cur.execute("""
                        INSERT INTO pipeline_stages (candidate_id, stage, notes, created_at)
                        VALUES (%s, 'contacted', 'Auto-added after vetting completion', NOW())
                        ON CONFLICT DO NOTHING
                    """, (candidate_id,))
                    
                    cur.execute("""
                        INSERT INTO activity_log (type, description, candidate_id, created_at)
                        VALUES ('vetting_complete', 'Candidate completed automated vetting', %s, NOW())
                    """, (candidate_id,))
                    
                    conn.commit()
                    
                    return f"""✅ Thanks {first_name}! You're all set.

We have your info and will reach out with matching opportunities soon!

Questions? Just reply to this message.

- ThrivingCare Team 💚"""
                
                else:
                    cur.execute("""
                        UPDATE candidate_vetting SET current_question = %s WHERE candidate_id = %s
                    """, (next_question, candidate_id))
                    conn.commit()
                    
                    next_q = VETTING_QUESTIONS[next_question - 1]
                    return f"""👍 Got it!

📋 Question {next_question}/5:
{next_q['question']}"""
                    
    except Exception as e:
        print(f"Error processing vetting response: {e}")
        return None


def handle_job_interest(candidate_id: int, phone: str, first_name: str):
    """Handle when candidate replies YES/INTERESTED to a job"""
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT jn.*, j.title, j.city, j.state, j.weekly_gross, j.facility, j.description
                    FROM job_notifications jn
                    JOIN jobs j ON jn.job_id = j.id
                    WHERE jn.candidate_id = %s
                    ORDER BY jn.sent_at DESC
                    LIMIT 1
                """, (candidate_id,))
                
                notification = cur.fetchone()
                
                if notification and twilio_client:
                    cur.execute("""
                        UPDATE job_notifications SET response = 'interested', responded_at = NOW()
                        WHERE id = %s
                    """, (notification['id'],))
                    
                    cur.execute("""
                        INSERT INTO pipeline_stages (candidate_id, job_id, stage, notes, created_at)
                        VALUES (%s, %s, 'contacted', 'Expressed interest via SMS', NOW())
                        ON CONFLICT DO NOTHING
                    """, (candidate_id, notification['job_id']))
                    
                    cur.execute("""
                        INSERT INTO activity_log (type, description, candidate_id, job_id, created_at)
                        VALUES ('job_interest', 'Candidate expressed interest via SMS', %s, %s, NOW())
                    """, (candidate_id, notification['job_id']))
                    
                    conn.commit()
                    
                    job = notification
                    message = f"""Great choice, {first_name}! 🎉

Here's more about the position:

📋 {job['title']}
🏥 {job['facility'] or 'Healthcare Facility'}
📍 {job['city']}, {job['state']}
💰 ${int(job['weekly_gross']):,}/week

{(job['description'] or '')[:200]}...

A recruiter will reach out within 24 hours to discuss next steps!

View all jobs: https://thrivingcarestaffing.com/jobs"""
                    
                    twilio_client.messages.create(
                        body=message,
                        from_=TWILIO_PHONE,
                        to=phone
                    )
                    
    except Exception as e:
        print(f"Error handling job interest: {e}")


@app.get("/api/jobs")
def get_jobs(
    specialty: Optional[str] = None,
    location: Optional[str] = None,
    page: int = 1,
    per_page: int = 20
):
    """Get job listings (paginated)"""
    
    try:
        offset = (page - 1) * per_page
        
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = "SELECT * FROM jobs WHERE active = TRUE"
                params = []
                
                if specialty:
                    query += " AND (title ILIKE %s OR discipline ILIKE %s)"
                    params.extend([f"%{specialty}%", f"%{specialty}%"])
                
                if location:
                    query += " AND (city ILIKE %s OR state ILIKE %s)"
                    params.extend([f"%{location}%", f"%{location}%"])
                
                query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
                params.extend([per_page, offset])
                
                cur.execute(query, params)
                jobs = cur.fetchall()
                
                cur.execute("SELECT COUNT(*) FROM jobs WHERE active = TRUE")
                total = cur.fetchone()['count']
        
        return {
            "jobs": jobs,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page
        }
        
    except Exception as e:
        print(f"Error getting jobs: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        # ============================================================================
# ADMIN: JOB CREATION
# ============================================================================

@app.post("/api/admin/jobs")
async def create_job(job: AdminJobCreate, x_admin_password: str = Header(None)):
    """Create a new job listing via admin panel"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        candidate_hourly = job.bill_rate * (1 - job.margin_percent / 100)
        weekly_gross = candidate_hourly * job.hours_per_week
        contract_total = weekly_gross * job.duration_weeks
        location = f"{job.city}, {job.state}"
        
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = """
                    INSERT INTO jobs (
                        title, discipline, facility, setting,
                        city, state, location,
                        duration_weeks, hours_per_week, shift, start_date,
                        bill_rate, margin_percent, hourly_rate, weekly_gross, contract_total,
                        description, requirements, benefits,
                        active, enriched, created_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        TRUE, TRUE, NOW()
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
        
        # AUTO-MATCHING: Find and notify matching candidates
        try:
            matches_notified = auto_match_job_to_candidates(
                new_job['id'], 
                job.title, 
                job.discipline,
                job.city,
                job.state,
                float(new_job['weekly_gross'])
            )
            print(f"  Notified {matches_notified} matching candidates")
        except Exception as e:
            print(f"Auto-match notification error: {e}")
        
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
                cur.execute("SELECT * FROM candidates WHERE id = %s", (candidate_id,))
                candidate = cur.fetchone()
                
                if not candidate:
                    raise HTTPException(status_code=404, detail="Candidate not found")
                
                cur.execute("""
                    SELECT title, city, state, weekly_gross 
                    FROM jobs 
                    WHERE active = TRUE AND enriched = TRUE
                    LIMIT 3
                """)
                jobs = cur.fetchall()
                
                if not jobs:
                    raise HTTPException(status_code=400, detail="No active jobs to send")
                
                job_list = "\n".join([f"• {j['title']} in {j['city']}, {j['state']} - ${int(j['weekly_gross'] or 0)}/wk" for j in jobs])
                message = f"""Hi {candidate['first_name']}! 🎉

New jobs matching your profile:

{job_list}

View all: https://thrivingcarestaffing.com/jobs

Reply STOP to unsubscribe."""

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
                    score = calculate_match_score(job, candidate)
                    if score > 0:
                        matches.append({
                            **dict(candidate),
                            'score': score
                        })
                
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
                
                cur.execute("SELECT * FROM jobs WHERE active = TRUE AND enriched = TRUE")
                jobs = cur.fetchall()
                
                matches = []
                for job in jobs:
                    score = calculate_match_score(job, candidate)
                    if score > 0:
                        matches.append({
                            **dict(job),
                            'score': score
                        })
                
                matches.sort(key=lambda x: x['score'], reverse=True)
                
                return {"matches": matches[:20]}
                
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error finding matches: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def calculate_match_score(job, candidate):
    """Calculate match score between job and candidate (0-100)"""
    score = 0
    
    job_discipline = (job.get('discipline') or '').lower()
    candidate_discipline = (candidate.get('license_type') or candidate.get('discipline') or '').lower()
    
    if job_discipline and candidate_discipline:
        if job_discipline == candidate_discipline:
            score += 50
        elif job_discipline in candidate_discipline or candidate_discipline in job_discipline:
            score += 30
    
    job_state = (job.get('state') or '').upper()
    candidate_state = (candidate.get('home_state') or '').upper()
    
    if job_state and candidate_state:
        if job_state == candidate_state:
            score += 30
    
    if candidate.get('resume_url'):
        score += 10
    
    if candidate.get('created_at'):
        from datetime import timedelta
        try:
            created = candidate['created_at']
            if isinstance(created, str):
                created = datetime.fromisoformat(created.replace('Z', '+00:00'))
            if datetime.now(created.tzinfo if created.tzinfo else None) - created < timedelta(days=30):
                score += 10
        except:
            pass
    
    return min(score, 100)


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
                           c.license_type, c.discipline AS candidate_discipline,
                           j.title AS job_title, j.city AS job_city, j.state AS job_state
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
                    SELECT id FROM pipeline_stages 
                    WHERE candidate_id = %s AND (job_id = %s OR (job_id IS NULL AND %s IS NULL))
                """, (entry.candidate_id, entry.job_id, entry.job_id))
                
                existing = cur.fetchone()
                if existing:
                    raise HTTPException(status_code=400, detail="Candidate already in pipeline for this job")
                
                cur.execute("""
                    INSERT INTO pipeline_stages (candidate_id, job_id, stage, created_at)
                    VALUES (%s, %s, %s, NOW())
                    RETURNING id
                """, (entry.candidate_id, entry.job_id, entry.stage))
                
                new_entry = cur.fetchone()
                
                cur.execute("""
                    INSERT INTO activity_log (type, description, candidate_id, job_id, created_at)
                    VALUES ('candidate_added', 'Candidate added to pipeline', %s, %s, NOW())
                """, (entry.candidate_id, entry.job_id))
                
                conn.commit()
                
                return {"success": True, "id": new_entry['id']}
                
    except HTTPException:
        raise
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
    
    valid_stages = ['new', 'contacted', 'submitted', 'interviewing', 'offered', 'placed']
    if stage_update.stage not in valid_stages:
        raise HTTPException(status_code=400, detail="Invalid stage")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM pipeline_stages WHERE id = %s", (entry_id,))
                entry = cur.fetchone()
                
                if not entry:
                    raise HTTPException(status_code=404, detail="Pipeline entry not found")
                
                old_stage = entry['stage']
                
                cur.execute("""
                    UPDATE pipeline_stages SET stage = %s WHERE id = %s
                """, (stage_update.stage, entry_id))
                
                cur.execute("""
                    INSERT INTO activity_log (type, description, candidate_id, job_id, created_at)
                    VALUES ('stage_change', %s, %s, %s, NOW())
                """, (f"Stage changed: {old_stage} → {stage_update.stage}", entry['candidate_id'], entry['job_id']))
                
                conn.commit()
                
                return {"success": True}
                
    except HTTPException:
        raise
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
                cur.execute("SELECT * FROM pipeline_stages WHERE id = %s", (entry_id,))
                entry = cur.fetchone()
                
                if not entry:
                    raise HTTPException(status_code=404, detail="Pipeline entry not found")
                
                current_notes = entry.get('notes') or ''
                new_notes = f"{current_notes}\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {note_data.note}".strip()
                
                cur.execute("""
                    UPDATE pipeline_stages SET notes = %s WHERE id = %s
                """, (new_notes, entry_id))
                
                cur.execute("""
                    INSERT INTO activity_log (type, description, candidate_id, job_id, created_at)
                    VALUES ('note_added', %s, %s, %s, NOW())
                """, (f"Note: {note_data.note[:50]}...", entry['candidate_id'], entry['job_id']))
                
                conn.commit()
                
                return {"success": True}
                
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error adding note: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/activity")
async def get_activity_log(
    x_admin_password: str = Header(None),
    limit: int = 20
):
    """Get recent activity log"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT * FROM activity_log
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (limit,))
                
                activities = cur.fetchall()
                
                return {"activities": activities}
                
    except Exception as e:
        print(f"Error fetching activity: {e}")
        raise HTTPException(status_code=500, detail=str(e))
