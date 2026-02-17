"""
ThrivingCare Website API v2.2
==============================
Backend API with AI-Powered Candidate Engagement & Vetting

FIXES from v2.1:
- Fixed /api/chat to use AI for responses (was broken)
- Fixed profile completion "undefined%" bug
- Added /api/quick-apply for job applications
- Added recruiter SMS alerts
- Added chat_messages table for conversation sync
"""

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Header, BackgroundTasks
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional, List
import os
from datetime import datetime
import re
import json
import psycopg2
from psycopg2.extras import RealDictCursor
import boto3
from twilio.rest import Client as TwilioClient
import anthropic

app = FastAPI(title="ThrivingCare API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://thrivingcarestaffing.com", "https://www.thrivingcarestaffing.com", "http://localhost:3000", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.getenv('DATABASE_URL')
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_PHONE = os.getenv('TWILIO_PHONE_NUMBER')
AWS_BUCKET = os.getenv('AWS_S3_BUCKET', 'thrivingcare-resumes')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
RECRUITER_PHONE = os.getenv('RECRUITER_PHONE')

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None
s3_client = boto3.client('s3') if os.getenv('AWS_ACCESS_KEY_ID') else None
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

class CandidateIntake(BaseModel):
    firstName: str
    lastName: str
    discipline: str
    specialty: str
    email: EmailStr
    phone: str
    homeAddress: str
    source: str = "website"

class QuickApply(BaseModel):
    firstName: str
    lastName: str
    discipline: str
    specialty: str
    email: EmailStr
    phone: str
    job_id: Optional[int] = None

class CandidateResponse(BaseModel):
    id: int
    message: str
    status: str

class AdminJobCreate(BaseModel):
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

class PayCalculatorRequest(BaseModel):
    bill_rate: float
    city: str
    state: str
    hours_per_week: int = 40
    is_travel_contract: bool = True
    gross_margin_pct: float = 0.20
    burden_pct: float = 0.20

class ChatMessage(BaseModel):
    message: str = ""
    candidate_id: Optional[int] = None
    session_id: Optional[str] = None

ADMIN_PASSWORD = "thrivingcare2024"

VETTING_QUESTIONS = [
    {"id": "licenses", "question": "What state(s) are you licensed in? (e.g., TX, CA, NY)", "field": "license_states", "step": 1},
    {"id": "experience", "question": "How many years of experience do you have in your field?", "field": "years_experience", "step": 2},
    {"id": "start_date", "question": "When are you available to start? (e.g., ASAP, 2 weeks, specific date)", "field": "available_date", "step": 3},
    {"id": "min_pay", "question": "What is your minimum weekly pay requirement? (Just the number, e.g., 2000)", "field": "min_weekly_pay", "step": 4},
    {"id": "travel", "question": "Are you open to travel/relocation for assignments? (Yes/No)", "field": "open_to_travel", "step": 5}
]

AI_SYSTEM_PROMPT = """You are a helpful recruiter assistant for ThrivingCare Staffing, a healthcare staffing agency specializing in travel nursing, mental health professionals (LCSW, LMFT, LPC, Psychologists), and school-based clinicians (SLPs, School Counselors).

Your role is to answer candidate questions about jobs with specific, accurate information. Be warm, professional, and concise (2-4 short paragraphs max). Use the candidate's first name naturally.

IMPORTANT: Always use REAL data from the job context - never make up pay rates, locations, or requirements. For pay questions, break down: taxable hourly, housing stipend (tax-free), M&IE stipend (tax-free)."""

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

GSA_RATES_FY2025 = {
    "Austin, TX": {"lodging": 166, "mie": 74}, "Dallas, TX": {"lodging": 161, "mie": 74},
    "Houston, TX": {"lodging": 156, "mie": 74}, "San Antonio, TX": {"lodging": 138, "mie": 69},
    "Los Angeles, CA": {"lodging": 209, "mie": 79}, "San Francisco, CA": {"lodging": 311, "mie": 79},
    "San Diego, CA": {"lodging": 194, "mie": 74}, "New York City, NY": {"lodging": 282, "mie": 79},
    "Chicago, IL": {"lodging": 231, "mie": 79}, "Miami, FL": {"lodging": 195, "mie": 79},
    "Atlanta, GA": {"lodging": 181, "mie": 79}, "Denver, CO": {"lodging": 198, "mie": 79},
    "Seattle, WA": {"lodging": 227, "mie": 79}, "Boston, MA": {"lodging": 268, "mie": 79},
    "Phoenix, AZ": {"lodging": 171, "mie": 74}, "Nashville, TN": {"lodging": 197, "mie": 79},
}
STANDARD_CONUS = {"lodging": 110, "mie": 68}

def get_gsa_rates_internal(city: str, state: str) -> dict:
    location_key = f"{city}, {state}"
    return GSA_RATES_FY2025.get(location_key, STANDARD_CONUS)

def generate_ai_response(candidate: dict, message: str, jobs: list) -> str:
    if not anthropic_client:
        return None
    
    job_texts = []
    for job in jobs[:5]:
        if not job: continue
        gsa = get_gsa_rates_internal(job.get('city', ''), job.get('state', ''))
        job_texts.append(f"JOB: {job.get('title')} in {job.get('city')}, {job.get('state')} - ${job.get('weekly_gross', 0):,.0f}/wk (${job.get('hourly_rate', 0):.2f}/hr + ${gsa['lodging']*7:,.0f} housing + ${gsa['mie']*5:,.0f} M&IE)")
    
    prompt = f"""CANDIDATE: {candidate.get('first_name', 'Unknown')} - {candidate.get('license_type') or candidate.get('discipline') or 'Healthcare'}

JOBS: {chr(10).join(job_texts) if job_texts else 'No jobs currently available'}

MESSAGE: "{message}"

Respond helpfully and concisely. Use their name."""

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=400, system=AI_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
    except Exception as e:
        print(f"AI error: {e}")
        return None

def get_fallback_response(message: str, first_name: str) -> str:
    return f"Hi {first_name}! Thanks for your message. A recruiter will follow up shortly.\n\nBrowse jobs: https://thrivingcarestaffing.com/jobs"

def send_recruiter_alert(candidate: dict, job: dict = None, alert_type: str = "new_application"):
    if not twilio_client or not RECRUITER_PHONE:
        return
    name = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}"
    discipline = candidate.get('discipline') or candidate.get('license_type') or 'Unknown'
    if alert_type == "new_application":
        msg = f"🔔 NEW APPLICATION!\n{name} ({discipline})\n📞 {candidate.get('phone')}\n📧 {candidate.get('email')}"
        if job: msg += f"\nApplied for: {job.get('title')} in {job.get('city')}, {job.get('state')}"
    else:
        msg = f"✅ VETTING COMPLETE!\n{name} ({discipline})\nReady for follow-up!"
    try:
        twilio_client.messages.create(body=msg, from_=TWILIO_PHONE, to=RECRUITER_PHONE)
    except: pass

@app.get("/")
def read_root():
    return {"status": "healthy", "service": "ThrivingCare API", "version": "2.2", "ai_enabled": anthropic_client is not None}

@app.get("/run-migrations")
def run_migrations():
    migrations = [
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS description TEXT",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS discipline VARCHAR(100)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS facility VARCHAR(255)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS setting VARCHAR(100)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS duration_weeks INTEGER DEFAULT 13",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS hours_per_week INTEGER DEFAULT 40",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS shift VARCHAR(50)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS bill_rate DECIMAL(10,2)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS margin_percent DECIMAL(5,2) DEFAULT 20",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS hourly_rate DECIMAL(10,2)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS weekly_gross DECIMAL(10,2)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS contract_total DECIMAL(10,2)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS benefits TEXT[]",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS requirements TEXT[]",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS enriched BOOLEAN DEFAULT FALSE",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source VARCHAR(100)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS start_date DATE",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS discipline VARCHAR(100)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS specialty VARCHAR(100)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS years_experience INTEGER",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS license_states TEXT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS available_date TEXT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS min_weekly_pay DECIMAL(10,2)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS open_to_travel BOOLEAN",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS ai_vetting_status VARCHAR(50) DEFAULT 'pending'",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS vetting_step INTEGER DEFAULT 0",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS resume_url TEXT",
        """CREATE TABLE IF NOT EXISTS applications (
            id SERIAL PRIMARY KEY, candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
            job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL, status VARCHAR(50) DEFAULT 'new',
            vetting_status VARCHAR(50) DEFAULT 'pending', vetting_step INTEGER DEFAULT 0,
            vetting_answers JSONB DEFAULT '{}', source VARCHAR(100) DEFAULT 'website',
            notes TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'new'",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS vetting_status VARCHAR(50) DEFAULT 'pending'",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS vetting_step INTEGER DEFAULT 0",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS vetting_answers JSONB DEFAULT '{}'",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS source VARCHAR(100) DEFAULT 'website'",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS notes TEXT",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
        """CREATE TABLE IF NOT EXISTS pipeline_stages (
            id SERIAL PRIMARY KEY, candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
            job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL, stage VARCHAR(50) NOT NULL,
            notes TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS ai_vetting_logs (
            id SERIAL PRIMARY KEY, candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
            question_id VARCHAR(50), question TEXT, response TEXT, step INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS chat_messages (
            id SERIAL PRIMARY KEY, candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
            sender VARCHAR(20) NOT NULL, message TEXT NOT NULL, channel VARCHAR(20) DEFAULT 'web',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS gsa_rates (
            id SERIAL PRIMARY KEY, city VARCHAR(100) NOT NULL, state VARCHAR(2) NOT NULL,
            daily_lodging DECIMAL(10,2) NOT NULL, daily_mie DECIMAL(10,2) NOT NULL,
            fiscal_year INTEGER DEFAULT 2025, UNIQUE(city, state, fiscal_year))""",
    ]
    results = []
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for m in migrations:
                try:
                    cur.execute(m)
                    conn.commit()
                    results.append({"success": True})
                except Exception as e:
                    conn.rollback()
                    results.append({"success": False, "error": str(e)})
    return {"message": "Migrations complete", "successful": len([r for r in results if r.get("success")]), "total": len(migrations)}

@app.get("/api/jobs/count")
def get_jobs_count():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM jobs WHERE active = TRUE")
                return {"count": cur.fetchone()[0]}
    except:
        return {"count": 0}

@app.get("/api/jobs")
def get_jobs(specialty: Optional[str] = None, location: Optional[str] = None, discipline: Optional[str] = None, page: int = 1, per_page: int = 20):
    try:
        offset = (page - 1) * per_page
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                query, params = "SELECT * FROM jobs WHERE active = TRUE", []
                if specialty:
                    query += " AND (specialty ILIKE %s OR title ILIKE %s)"
                    params.extend([f"%{specialty}%", f"%{specialty}%"])
                if location:
                    query += " AND (city ILIKE %s OR state ILIKE %s)"
                    params.extend([f"%{location}%", f"%{location}%"])
                if discipline:
                    query += " AND discipline ILIKE %s"
                    params.append(f"%{discipline}%")
                query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
                params.extend([per_page, offset])
                cur.execute(query, params)
                jobs = cur.fetchall()
                cur.execute("SELECT COUNT(*) as count FROM jobs WHERE active = TRUE")
                total = cur.fetchone()['count']
                return {"jobs": jobs, "page": page, "per_page": per_page, "total": total}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
            job = cur.fetchone()
            if not job: raise HTTPException(status_code=404, detail="Job not found")
            gsa = get_gsa_rates_internal(job.get('city', ''), job.get('state', ''))
            job['weekly_housing_stipend'] = gsa['lodging'] * 7
            job['weekly_mie_stipend'] = gsa['mie'] * 5
            return job

@app.post("/api/quick-apply")
async def quick_apply(application: QuickApply, background_tasks: BackgroundTasks):
    """Quick apply to a job - AI collects remaining info via SMS"""
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM candidates WHERE phone = %s OR email = %s", (application.phone, application.email))
                existing = cur.fetchone()
                if existing:
                    candidate_id = existing['id']
                    cur.execute("UPDATE candidates SET first_name=%s, last_name=%s, license_type=%s, specialty=%s, vetting_step=COALESCE(vetting_step,1), ai_vetting_status=CASE WHEN ai_vetting_status='completed' THEN 'completed' ELSE 'in_progress' END WHERE id=%s",
                               (application.firstName, application.lastName, application.discipline, application.specialty, candidate_id))
                else:
                    cur.execute("INSERT INTO candidates (first_name, last_name, email, phone, license_type, specialty, active, vetting_step, ai_vetting_status, created_at) VALUES (%s,%s,%s,%s,%s,%s,TRUE,1,'in_progress',NOW()) RETURNING id",
                               (application.firstName, application.lastName, application.email, application.phone, application.discipline, application.specialty))
                    candidate_id = cur.fetchone()['id']
                job = None
                if application.job_id:
                    cur.execute("SELECT * FROM jobs WHERE id = %s", (application.job_id,))
                    job = cur.fetchone()
                cur.execute("INSERT INTO applications (candidate_id, job_id, status, vetting_status, vetting_step, source, created_at) VALUES (%s,%s,'new','in_progress',1,'quick_apply',NOW()) RETURNING id", (candidate_id, application.job_id))
                application_id = cur.fetchone()['id']
                cur.execute("INSERT INTO pipeline_stages (candidate_id, job_id, stage, notes, created_at) VALUES (%s,%s,'new_application','Quick apply',NOW())", (candidate_id, application.job_id))
                conn.commit()
                cur.execute("SELECT * FROM candidates WHERE id = %s", (candidate_id,))
                candidate = cur.fetchone()
        background_tasks.add_task(send_recruiter_alert, dict(candidate), dict(job) if job else None)
        first_q = VETTING_QUESTIONS[0]
        msg = f"Hi {application.firstName}! 🎉 Thanks for applying"
        if job: msg += f" to {job['title']} in {job['city']}, {job['state']}"
        msg += f"!\n\nLet me ask a few quick questions:\n\n{first_q['question']}"
        if twilio_client:
            try: twilio_client.messages.create(body=msg, from_=TWILIO_PHONE, to=application.phone)
            except: pass
        return {"success": True, "candidate_id": candidate_id, "application_id": application_id, "message": "Check your phone!", "first_question": first_q['question']}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/candidates", response_model=CandidateResponse)
async def create_candidate(candidate: CandidateIntake, background_tasks: BackgroundTasks):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("INSERT INTO candidates (first_name, last_name, email, phone, home_address, license_type, specialty, active, vetting_step, ai_vetting_status, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,1,'in_progress',NOW()) RETURNING id",
                           (candidate.firstName, candidate.lastName, candidate.email, candidate.phone, candidate.homeAddress, candidate.discipline, candidate.specialty))
                candidate_id = cur.fetchone()['id']
                cur.execute("INSERT INTO applications (candidate_id, status, vetting_status, vetting_step, source, created_at) VALUES (%s,'new','in_progress',1,'website',NOW())", (candidate_id,))
                cur.execute("INSERT INTO pipeline_stages (candidate_id, stage, notes, created_at) VALUES (%s,'new_application','Website signup',NOW())", (candidate_id,))
                conn.commit()
                cur.execute("SELECT * FROM candidates WHERE id = %s", (candidate_id,))
                new_candidate = cur.fetchone()
        background_tasks.add_task(send_recruiter_alert, dict(new_candidate), None)
        if twilio_client:
            msg = f"Hi {candidate.firstName}! 🎉 Welcome to ThrivingCare!\n\n{VETTING_QUESTIONS[0]['question']}"
            try: twilio_client.messages.create(body=msg, from_=TWILIO_PHONE, to=candidate.phone)
            except: pass
        return CandidateResponse(id=candidate_id, message="Welcome! Check your phone.", status="success")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/candidates/{candidate_id}/profile-completion")
async def get_profile_completion(candidate_id: int):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM candidates WHERE id = %s", (candidate_id,))
                c = cur.fetchone()
                if not c: raise HTTPException(status_code=404, detail="Not found")
                fields = [c.get('first_name'), c.get('last_name'), c.get('email'), c.get('phone'), c.get('license_type'), c.get('license_states'), c.get('years_experience'), c.get('available_date'), c.get('min_weekly_pay'), c.get('open_to_travel')]
                completed = sum(1 for f in fields if f is not None and f != '')
                pct = int((completed / len(fields)) * 100) if fields else 0
                return {"candidate_id": candidate_id, "completion_percentage": pct, "is_complete": pct >= 60, "vetting_status": c.get('ai_vetting_status') or 'pending', "vetting_step": c.get('vetting_step') or 0}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/chat")
async def chat_with_candidate(chat: ChatMessage):
    """AI chat endpoint - FIXED to use AI"""
    try:
        candidate_data = None
        if chat.candidate_id:
            with get_db_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT * FROM candidates WHERE id = %s", (chat.candidate_id,))
                    candidate_data = cur.fetchone()
        if not candidate_data:
            return {"response": "Welcome! 👋 I'm here to help you find healthcare positions.\n\nFirst, what's your name?", "profile_completion": 0}
        
        first_name = candidate_data.get('first_name', 'there')
        vetting_status = candidate_data.get('ai_vetting_status') or 'pending'
        vetting_step = candidate_data.get('vetting_step') or 0
        
        fields = [candidate_data.get('first_name'), candidate_data.get('last_name'), candidate_data.get('email'), candidate_data.get('phone'), candidate_data.get('license_type'), candidate_data.get('license_states'), candidate_data.get('years_experience'), candidate_data.get('available_date'), candidate_data.get('min_weekly_pay'), candidate_data.get('open_to_travel')]
        profile_completion = int((sum(1 for f in fields if f is not None and f != '') / len(fields)) * 100)
        
        if not chat.message or chat.message.strip() == '':
            if vetting_status == 'completed':
                return {"response": f"Welcome back, {first_name}! 😊 How can I help?", "profile_completion": profile_completion, "vetting_status": vetting_status}
            else:
                q = VETTING_QUESTIONS[vetting_step - 1] if vetting_step > 0 and vetting_step <= len(VETTING_QUESTIONS) else VETTING_QUESTIONS[0]
                return {"response": f"Welcome back! Let's continue:\n\n{q['question']}", "profile_completion": profile_completion, "vetting_status": vetting_status}
        
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                discipline = candidate_data.get('license_type') or ''
                cur.execute("SELECT * FROM jobs WHERE active = TRUE ORDER BY created_at DESC LIMIT 5")
                jobs = cur.fetchall()
        
        is_question = '?' in chat.message or any(chat.message.lower().startswith(w) for w in ['what', 'where', 'when', 'how', 'why', 'can', 'do', 'is', 'are'])
        
        if vetting_status == 'in_progress' and vetting_step > 0 and vetting_step <= len(VETTING_QUESTIONS) and not is_question:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    q = VETTING_QUESTIONS[vetting_step - 1]
                    field = q['field']
                    if field == 'license_states':
                        states = re.findall(r'\b([A-Z]{2})\b', chat.message.upper())
                        cur.execute("UPDATE candidates SET license_states = %s WHERE id = %s", (','.join(states) if states else chat.message, chat.candidate_id))
                    elif field == 'years_experience':
                        nums = re.findall(r'\d+', chat.message)
                        if nums: cur.execute("UPDATE candidates SET years_experience = %s WHERE id = %s", (int(nums[0]), chat.candidate_id))
                    elif field == 'available_date':
                        cur.execute("UPDATE candidates SET available_date = %s WHERE id = %s", (chat.message, chat.candidate_id))
                    elif field == 'min_weekly_pay':
                        nums = re.findall(r'[\d,]+', chat.message)
                        if nums: cur.execute("UPDATE candidates SET min_weekly_pay = %s WHERE id = %s", (int(nums[0].replace(',','')), chat.candidate_id))
                    elif field == 'open_to_travel':
                        cur.execute("UPDATE candidates SET open_to_travel = %s WHERE id = %s", (chat.message.upper() in ['YES','Y','YEAH','YEP','SURE'], chat.candidate_id))
                    
                    next_step = vetting_step + 1
                    if next_step > len(VETTING_QUESTIONS):
                        cur.execute("UPDATE candidates SET vetting_step = %s, ai_vetting_status = 'completed' WHERE id = %s", (next_step, chat.candidate_id))
                        cur.execute("UPDATE applications SET vetting_status = 'completed', status = 'vetted' WHERE candidate_id = %s", (chat.candidate_id,))
                        response = f"Excellent, {first_name}! ✅🎉\n\nYour profile is complete! A recruiter will reach out soon."
                        profile_completion = 100
                    else:
                        cur.execute("UPDATE candidates SET vetting_step = %s WHERE id = %s", (next_step, chat.candidate_id))
                        response = f"Got it! ✅\n\n{VETTING_QUESTIONS[next_step - 1]['question']}"
                        profile_completion = int((next_step / len(VETTING_QUESTIONS)) * 60) + 40
                    conn.commit()
            return {"response": response, "profile_completion": profile_completion, "vetting_status": "completed" if next_step > len(VETTING_QUESTIONS) else "in_progress"}
        
        response = generate_ai_response(dict(candidate_data), chat.message, [dict(j) for j in jobs] if jobs else [])
        if not response:
            response = get_fallback_response(chat.message, first_name)
        return {"response": response, "profile_completion": profile_completion, "vetting_status": vetting_status}
    except Exception as e:
        print(f"Chat error: {e}")
        return {"response": "Sorry, please try again!", "profile_completion": 0}

@app.get("/api/chat/history/{candidate_id}")
async def get_chat_history(candidate_id: int):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM chat_messages WHERE candidate_id = %s ORDER BY created_at ASC LIMIT 100", (candidate_id,))
                messages = cur.fetchall()
                cur.execute("SELECT first_name, last_name, ai_vetting_status, vetting_step FROM candidates WHERE id = %s", (candidate_id,))
                c = cur.fetchone()
                return {"messages": [dict(m) for m in messages] if messages else [], "candidate_name": f"{c['first_name']} {c['last_name']}" if c else "Unknown", "vetting_status": c.get('ai_vetting_status') if c else None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/sms/webhook")
async def handle_incoming_sms(request: Request, background_tasks: BackgroundTasks):
    """Twilio SMS webhook with AI vetting"""
    try:
        form_data = await request.form()
        from_number, message_body = form_data.get('From', ''), form_data.get('Body', '').strip()
        print(f"📱 SMS from {from_number}: {message_body}")
        
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM candidates WHERE phone = %s", (from_number,))
                candidate = cur.fetchone()
                if not candidate:
                    return Response(content="", media_type="text/xml")
                
                candidate_id, first_name = candidate['id'], candidate['first_name']
                msg_upper = message_body.upper().strip()
                response_msg = None
                
                if msg_upper in ['STOP', 'UNSUBSCRIBE']:
                    cur.execute("UPDATE candidates SET active = FALSE WHERE id = %s", (candidate_id,))
                    conn.commit()
                    if twilio_client: twilio_client.messages.create(body="Unsubscribed. Reply START to resubscribe.", from_=TWILIO_PHONE, to=from_number)
                    return Response(content="", media_type="text/xml")
                
                elif msg_upper in ['START', 'SUBSCRIBE']:
                    cur.execute("UPDATE candidates SET active = TRUE WHERE id = %s", (candidate_id,))
                    conn.commit()
                    response_msg = f"Welcome back, {first_name}! 🎉"
                
                elif candidate.get('ai_vetting_status') == 'in_progress':
                    step = candidate.get('vetting_step', 1) or 1
                    if step <= len(VETTING_QUESTIONS):
                        q = VETTING_QUESTIONS[step - 1]
                        is_question = '?' in message_body or any(message_body.lower().startswith(w) for w in ['what','where','when','how','why','can','do','is'])
                        
                        if is_question:
                            cur.execute("SELECT * FROM jobs WHERE active = TRUE LIMIT 5")
                            jobs = cur.fetchall()
                            ai_answer = generate_ai_response(dict(candidate), message_body, [dict(j) for j in jobs] if jobs else [])
                            response_msg = f"{ai_answer or 'Great question!'}\n\n---\nTo continue: {q['question']}"
                        else:
                            field = q['field']
                            if field == 'license_states':
                                states = re.findall(r'\b([A-Z]{2})\b', message_body.upper())
                                cur.execute("UPDATE candidates SET license_states = %s WHERE id = %s", (','.join(states) if states else message_body, candidate_id))
                            elif field == 'years_experience':
                                nums = re.findall(r'\d+', message_body)
                                if nums: cur.execute("UPDATE candidates SET years_experience = %s WHERE id = %s", (int(nums[0]), candidate_id))
                            elif field == 'available_date':
                                cur.execute("UPDATE candidates SET available_date = %s WHERE id = %s", (message_body, candidate_id))
                            elif field == 'min_weekly_pay':
                                nums = re.findall(r'[\d,]+', message_body.replace(',',''))
                                if nums: cur.execute("UPDATE candidates SET min_weekly_pay = %s WHERE id = %s", (int(nums[0]), candidate_id))
                            elif field == 'open_to_travel':
                                cur.execute("UPDATE candidates SET open_to_travel = %s WHERE id = %s", (msg_upper in ['YES','Y','YEAH','YEP','SURE','OK'], candidate_id))
                            
                            cur.execute("INSERT INTO ai_vetting_logs (candidate_id, question_id, question, response, step, created_at) VALUES (%s,%s,%s,%s,%s,NOW())", (candidate_id, q['id'], q['question'], message_body, step))
                            
                            next_step = step + 1
                            if next_step > len(VETTING_QUESTIONS):
                                cur.execute("UPDATE candidates SET vetting_step = %s, ai_vetting_status = 'completed' WHERE id = %s", (next_step, candidate_id))
                                cur.execute("UPDATE applications SET vetting_status = 'completed', status = 'vetted' WHERE candidate_id = %s", (candidate_id,))
                                response_msg = f"Excellent, {first_name}! ✅🎉\n\nProfile complete! A recruiter will reach out soon.\n\nBrowse jobs: thrivingcarestaffing.com/jobs"
                                cur.execute("SELECT * FROM candidates WHERE id = %s", (candidate_id,))
                                background_tasks.add_task(send_recruiter_alert, dict(cur.fetchone()), None, "vetting_complete")
                            else:
                                cur.execute("UPDATE candidates SET vetting_step = %s WHERE id = %s", (next_step, candidate_id))
                                response_msg = f"Got it! ✅\n\n{VETTING_QUESTIONS[next_step - 1]['question']}"
                        conn.commit()
                
                elif msg_upper == 'HELP':
                    response_msg = f"Hi {first_name}! Ask me about jobs, pay, locations.\nReply STOP to unsubscribe."
                
                else:
                    cur.execute("SELECT * FROM jobs WHERE active = TRUE LIMIT 5")
                    jobs = cur.fetchall()
                    response_msg = generate_ai_response(dict(candidate), message_body, [dict(j) for j in jobs] if jobs else [])
                    if not response_msg: response_msg = get_fallback_response(message_body, first_name)
                
                if response_msg and twilio_client:
                    if len(response_msg) > 1500: response_msg = response_msg[:1450] + "..."
                    twilio_client.messages.create(body=response_msg, from_=TWILIO_PHONE, to=from_number)
        
        return Response(content="", media_type="text/xml")
    except Exception as e:
        print(f"SMS error: {e}")
        return Response(content="", media_type="text/xml")

@app.get("/api/admin/applications")
async def get_applications(status: Optional[str] = None, x_admin_password: str = Header(None)):
    if x_admin_password != ADMIN_PASSWORD: raise HTTPException(status_code=401, detail="Unauthorized")
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = "SELECT a.*, c.first_name, c.last_name, c.email, c.phone, c.license_type, c.license_states, c.years_experience, j.title as job_title, j.city as job_city, j.state as job_state FROM applications a JOIN candidates c ON a.candidate_id = c.id LEFT JOIN jobs j ON a.job_id = j.id"
            if status: 
                cur.execute(query + " WHERE a.status = %s ORDER BY a.created_at DESC", (status,))
            else:
                cur.execute(query + " ORDER BY a.created_at DESC")
            return {"applications": cur.fetchall()}

@app.put("/api/admin/applications/{application_id}/status")
async def update_application_status(application_id: int, status: str, x_admin_password: str = Header(None)):
    if x_admin_password != ADMIN_PASSWORD: raise HTTPException(status_code=401, detail="Unauthorized")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE applications SET status = %s, updated_at = NOW() WHERE id = %s", (status, application_id))
            conn.commit()
    return {"success": True}

@app.get("/api/admin/candidates")
async def get_candidates(discipline: Optional[str] = None, x_admin_password: str = Header(None)):
    if x_admin_password != ADMIN_PASSWORD: raise HTTPException(status_code=401, detail="Unauthorized")
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = "SELECT * FROM candidates WHERE active = TRUE"
            if discipline:
                cur.execute(query + " AND license_type ILIKE %s ORDER BY created_at DESC", (f"%{discipline}%",))
            else:
                cur.execute(query + " ORDER BY created_at DESC")
            return {"candidates": cur.fetchall()}

@app.get("/api/admin/analytics")
async def get_analytics(x_admin_password: str = Header(None)):
    if x_admin_password != ADMIN_PASSWORD: raise HTTPException(status_code=401, detail="Unauthorized")
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) as count FROM jobs WHERE active = TRUE")
            total_jobs = cur.fetchone()['count']
            cur.execute("SELECT COUNT(*) as count FROM candidates WHERE active = TRUE")
            total_candidates = cur.fetchone()['count']
            cur.execute("SELECT COUNT(*) as count FROM applications WHERE status = 'new'")
            new_apps = cur.fetchone()['count']
            cur.execute("SELECT COUNT(*) as count FROM applications WHERE vetting_status = 'completed'")
            vetted = cur.fetchone()['count']
            return {"total_jobs": total_jobs, "total_candidates": total_candidates, "new_applications": new_apps, "vetted_applications": vetted}

@app.post("/api/admin/jobs")
async def create_job_admin(job: AdminJobCreate, x_admin_password: str = Header(None)):
    if x_admin_password != ADMIN_PASSWORD: raise HTTPException(status_code=401, detail="Unauthorized")
    hourly = job.bill_rate * (1 - job.margin_percent / 100)
    weekly = hourly * job.hours_per_week
    total = weekly * job.duration_weeks
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""INSERT INTO jobs (title, discipline, facility, setting, city, state, duration_weeks, hours_per_week, shift, start_date, bill_rate, margin_percent, hourly_rate, weekly_gross, contract_total, description, requirements, benefits, active, enriched, source, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,TRUE,'admin',NOW()) RETURNING id""",
                (job.title, job.discipline, job.facility, job.setting, job.city, job.state, job.duration_weeks, job.hours_per_week, job.shift, job.start_date, job.bill_rate, job.margin_percent, round(hourly,2), round(weekly,2), round(total,2), job.description, job.requirements, job.benefits))
            job_id = cur.fetchone()['id']
            conn.commit()
    return {"success": True, "job_id": job_id}

@app.put("/api/admin/jobs/{job_id}/status")
async def update_job_status(job_id: int, status: JobStatusUpdate, x_admin_password: str = Header(None)):
    if x_admin_password != ADMIN_PASSWORD: raise HTTPException(status_code=401, detail="Unauthorized")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE jobs SET active = %s WHERE id = %s", (status.active, job_id))
            conn.commit()
    return {"success": True}

@app.delete("/api/admin/jobs/{job_id}")
async def delete_job(job_id: int, x_admin_password: str = Header(None)):
    if x_admin_password != ADMIN_PASSWORD: raise HTTPException(status_code=401, detail="Unauthorized")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
            conn.commit()
    return {"success": True}

@app.get("/api/admin/pipeline")
async def get_pipeline(job_id: Optional[int] = None, x_admin_password: str = Header(None)):
    if x_admin_password != ADMIN_PASSWORD: raise HTTPException(status_code=401, detail="Unauthorized")
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = "SELECT ps.*, c.first_name, c.last_name, c.email, c.phone, j.title as job_title FROM pipeline_stages ps JOIN candidates c ON ps.candidate_id = c.id LEFT JOIN jobs j ON ps.job_id = j.id"
            if job_id:
                cur.execute(query + " WHERE ps.job_id = %s ORDER BY ps.created_at DESC", (job_id,))
            else:
                cur.execute(query + " ORDER BY ps.created_at DESC")
            return {"entries": cur.fetchall()}

@app.post("/api/admin/pipeline")
async def add_to_pipeline(entry: PipelineCreate, x_admin_password: str = Header(None)):
    if x_admin_password != ADMIN_PASSWORD: raise HTTPException(status_code=401, detail="Unauthorized")
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO pipeline_stages (candidate_id, job_id, stage, created_at) VALUES (%s,%s,%s,NOW()) RETURNING id", (entry.candidate_id, entry.job_id, entry.stage))
            return {"success": True, "id": cur.fetchone()['id']}

@app.put("/api/admin/pipeline/{entry_id}/stage")
async def update_pipeline_stage(entry_id: int, stage_update: PipelineStageUpdate, x_admin_password: str = Header(None)):
    if x_admin_password != ADMIN_PASSWORD: raise HTTPException(status_code=401, detail="Unauthorized")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE pipeline_stages SET stage = %s WHERE id = %s", (stage_update.stage, entry_id))
            conn.commit()
    return {"success": True}

@app.post("/api/calculate-pay")
def calculate_pay_package(request: PayCalculatorRequest):
    gsa = get_gsa_rates_internal(request.city, request.state)
    weekly_revenue = request.bill_rate * request.hours_per_week
    margin = weekly_revenue * request.gross_margin_pct
    burden = (weekly_revenue - margin) * request.burden_pct
    available = weekly_revenue - margin - burden
    if request.is_travel_contract:
        housing, mie = gsa['lodging'] * 7, gsa['mie'] * 5
        taxable = max(available - housing - mie, 15 * request.hours_per_week)
        return {"contract_type": "Travel", "weekly_taxable": round(taxable,2), "weekly_housing": round(housing,2), "weekly_mie": round(mie,2), "total_weekly": round(taxable + housing + mie,2), "hourly_taxable": round(taxable/request.hours_per_week,2)}
    return {"contract_type": "Local", "weekly_taxable": round(available,2), "hourly_taxable": round(available/request.hours_per_week,2)}

@app.get("/api/gsa-rates")
def get_gsa_rates_endpoint(city: str, state: str):
    rates = get_gsa_rates_internal(city, state)
    return {"city": city, "state": state, "daily_lodging": rates["lodging"], "daily_mie": rates["mie"], "weekly_lodging": rates["lodging"]*7, "weekly_mie": rates["mie"]*5}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
