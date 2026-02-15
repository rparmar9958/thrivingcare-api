"""
ThrivingCare Website API v2.1
==============================

Backend API with AI-Powered Candidate Engagement & Vetting

Endpoints:
- POST /api/candidates - Create new candidate from website (starts vetting)
- POST /api/candidates/{id}/resume - Upload resume
- GET /api/jobs/count - Get total active jobs
- GET /api/jobs - Get job listings (paginated)
- GET /run-migrations - Run database migrations
- POST /api/calculate-pay - Calculate pay package from bill rate
- GET /api/gsa-rates - Get GSA per diem rates for a location
- POST /api/sms/webhook - Twilio webhook with AI vetting
- GET /api/admin/applications - View all applications
- Admin endpoints for job/candidate management
"""

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Header
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from decimal import Decimal
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
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')

# Initialize services
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None
s3_client = boto3.client('s3') if os.getenv('AWS_ACCESS_KEY_ID') else None
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


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


# Admin password - CHANGE THIS IN PRODUCTION!
ADMIN_PASSWORD = "thrivingcare2024"

# ============================================================================
# VETTING QUESTIONS - Asked in sequence via SMS
# ============================================================================

VETTING_QUESTIONS = [
    {
        "id": "licenses", 
        "question": "What state(s) are you licensed in? (e.g., TX, CA, NY)", 
        "field": "license_states",
        "step": 1
    },
    {
        "id": "experience", 
        "question": "How many years of experience do you have in your field?", 
        "field": "years_experience",
        "step": 2
    },
    {
        "id": "start_date", 
        "question": "When are you available to start? (e.g., ASAP, 2 weeks, specific date)", 
        "field": "available_date",
        "step": 3
    },
    {
        "id": "min_pay", 
        "question": "What is your minimum weekly pay requirement? (Just the number, e.g., 2000)", 
        "field": "min_weekly_pay",
        "step": 4
    },
    {
        "id": "travel", 
        "question": "Are you open to travel/relocation for assignments? (Yes/No)", 
        "field": "open_to_travel",
        "step": 5
    }
]


# ============================================================================
# AI ENGAGEMENT SYSTEM
# ============================================================================

AI_SYSTEM_PROMPT = """You are a helpful recruiter assistant for ThrivingCare Staffing, a healthcare staffing agency specializing in travel nursing, mental health professionals (LCSW, LMFT, LPC, Psychologists), and school-based clinicians (SLPs, School Counselors).

Your role is to:
1. Answer candidate questions about jobs with specific, accurate information from the job data provided
2. Be warm, professional, and helpful
3. Use the candidate's first name naturally
4. Keep responses concise (SMS format - aim for 2-4 short paragraphs max)
5. Guide candidates toward next steps when appropriate

IMPORTANT RULES:
- Always use REAL data from the job/candidate context - never make up pay rates, locations, or requirements
- If you don't have specific info, say so honestly and offer to connect them with a recruiter
- For pay questions, break down the components: taxable hourly, housing stipend, M&IE stipend
- Housing and M&IE stipends are TAX-FREE (based on GSA per diem rates)
- When discussing licensing, mention that travel positions require a license in the job's state
- Be encouraging but honest about requirements

ESCALATION TRIGGERS - Suggest connecting with a recruiter when:
- Candidate asks about negotiating pay
- Candidate has complex scheduling needs  
- Candidate asks about benefits details (health insurance, 401k specifics)
- Candidate wants to formally apply
- Candidate seems frustrated or confused
- Question requires information you don't have

Always end with a clear next step or question to keep the conversation moving."""


def generate_ai_response(candidate: dict, message: str, jobs: list) -> str:
    """Generate an AI response to a candidate's SMS message using Claude."""
    
    if not anthropic_client:
        return None
    
    # Build candidate context
    candidate_context = f"""
CANDIDATE PROFILE:
- Name: {candidate.get('first_name', 'Unknown')} {candidate.get('last_name', '')}
- Discipline: {candidate.get('license_type') or candidate.get('discipline') or 'Not specified'}
- Home Location: {candidate.get('home_city', '')}, {candidate.get('home_state', '')}
- License States: {candidate.get('license_states') or 'Not verified yet'}
"""
    
    # Build job context
    job_texts = []
    for job in jobs[:5]:
        if not job:
            continue
        
        # Get GSA rates for this job's location
        gsa = get_gsa_rates_internal(job.get('city', ''), job.get('state', ''))
        weekly_housing = gsa.get('lodging', 110) * 7
        weekly_mie = gsa.get('mie', 68) * 5
        
        job_text = f"""
JOB #{job.get('id')}:
- Title: {job.get('title')}
- Facility: {job.get('facility', 'Healthcare Facility')}
- Location: {job.get('city')}, {job.get('state')}
- Setting: {job.get('setting', 'Not specified')}
- Discipline: {job.get('discipline', 'Healthcare')}
- Contract: {job.get('duration_weeks', 13)} weeks, {job.get('hours_per_week', 40)} hrs/wk
- Shift: {job.get('shift', 'Days')}
- Start Date: {job.get('start_date') or 'ASAP/Flexible'}
- Weekly Gross Pay: ${job.get('weekly_gross', 0):,.0f}
- Hourly Rate (taxable): ${job.get('hourly_rate', 0):.2f}
- Housing Stipend: ${weekly_housing:,.0f}/week (tax-free, based on GSA rates)
- M&IE Stipend: ${weekly_mie:,.0f}/week (tax-free, based on GSA rates)
- Contract Total: ${job.get('contract_total', 0):,.0f}
- Description: {(job.get('description') or 'Contact recruiter for details.')[:300]}
"""
        job_texts.append(job_text)
    
    job_context = "\n---\n".join(job_texts) if job_texts else "NO MATCHING JOBS CURRENTLY AVAILABLE"
    
    # Build the prompt
    user_prompt = f"""
{candidate_context}

AVAILABLE JOBS:
{job_context}

---

CANDIDATE'S MESSAGE: "{message}"

Generate a helpful, concise SMS response (2-4 short paragraphs max). Use the specific job data above to answer their question accurately. Address them by their first name ({candidate.get('first_name', 'there')}).
"""

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=AI_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}]
        )
        return response.content[0].text
    except Exception as e:
        print(f"AI response generation error: {e}")
        return None


def get_fallback_response(message: str, first_name: str) -> str:
    """Fallback response when AI is unavailable."""
    if '?' in message:
        return f"""Hi {first_name}! Thanks for your question.

A recruiter will follow up with more details within 24 hours.

Browse all jobs: https://thrivingcarestaffing.com/jobs

- ThrivingCare Team 💚"""
    else:
        return f"""Hi {first_name}! Thanks for reaching out.

A recruiter will follow up with you shortly.

Questions? Just reply to this message!

- ThrivingCare Team 💚"""


# ============================================================================
# ADDRESS PARSER
# ============================================================================

def parse_address(full_address: str) -> dict:
    """Parse a full address into components"""
    
    result = {
        'street': '',
        'city': '',
        'state': '',
        'zip_code': ''
    }
    
    if not full_address:
        return result
    
    # Pattern for ZIP code
    zip_match = re.search(r'\b(\d{5}(?:-\d{4})?)\b', full_address)
    if zip_match:
        result['zip_code'] = zip_match.group(1)
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


# ============================================================================
# RESUME PARSING
# ============================================================================

def parse_resume_text(text: str) -> dict:
    """Extract key information from resume text"""
    
    extracted = {
        'licenses': [],
        'certifications': [],
        'years_experience': None,
        'specialties': [],
        'email': None,
        'phone': None,
        'license_states': []
    }
    
    # Extract email
    email_match = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
    if email_match:
        extracted['email'] = email_match[0]
    
    # Extract phone
    phone_match = re.findall(r'[\(]?\d{3}[\)]?[-.\s]?\d{3}[-.\s]?\d{4}', text)
    if phone_match:
        extracted['phone'] = phone_match[0]
    
    # Extract licenses (RN, LPN, LCSW, LMFT, etc.)
    license_pattern = r'\b(RN|LPN|LVN|LCSW|LMFT|LICSW|LPC|NP|PA-C|PMHNP|FNP|CNS)\b'
    licenses = re.findall(license_pattern, text, re.IGNORECASE)
    extracted['licenses'] = list(set([l.upper() for l in licenses]))
    
    # Extract state licenses
    state_pattern = r'\b(TX|CA|NY|FL|IL|PA|OH|GA|NC|MI|NJ|VA|WA|AZ|MA|TN|IN|MO|MD|WI|CO|MN|SC|AL|LA|KY|OR|OK|CT|UT|IA|NV|AR|MS|KS|NM|NE|WV|ID|HI|NH|ME|MT|RI|DE|SD|ND|AK|VT|WY|DC)\b'
    states = re.findall(state_pattern, text, re.IGNORECASE)
    extracted['license_states'] = list(set([s.upper() for s in states]))
    
    # Extract certifications
    cert_pattern = r'\b(BLS|ACLS|PALS|NRP|TNCC|ENPC|CEN|CCRN|OCN|CNOR|RNC|CMSRN)\b'
    certs = re.findall(cert_pattern, text, re.IGNORECASE)
    extracted['certifications'] = list(set([c.upper() for c in certs]))
    
    # Extract years of experience
    exp_pattern = r'(\d+)\+?\s*(?:years?|yrs?)\s*(?:of)?\s*(?:experience|exp)'
    exp_match = re.search(exp_pattern, text, re.IGNORECASE)
    if exp_match:
        extracted['years_experience'] = int(exp_match.group(1))
    
    # Extract specialties
    specialty_pattern = r'\b(ICU|CCU|ER|ED|OR|PACU|L&D|NICU|PICU|Med-?Surg|Telemetry|Oncology|Cardiac|Neuro|Ortho|Psych|Mental Health|Behavioral Health|Pediatric|Geriatric|Home Health|Hospice|Dialysis)\b'
    specialties = re.findall(specialty_pattern, text, re.IGNORECASE)
    extracted['specialties'] = list(set([s.title() for s in specialties]))
    
    return extracted


# ============================================================================
# DATABASE HELPERS
# ============================================================================

def get_db_connection():
    """Get database connection"""
    return psycopg2.connect(DATABASE_URL)


# ============================================================================
# GSA RATES
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

# Standard CONUS rate for unlisted locations
STANDARD_CONUS = {"lodging": 110, "mie": 68}


def get_gsa_rates_internal(city: str, state: str) -> dict:
    """Get GSA per diem rates for a location (internal use)."""
    
    # Try database first
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT daily_lodging, daily_mie FROM gsa_rates 
                    WHERE LOWER(city) = LOWER(%s) AND LOWER(state) = LOWER(%s) 
                    AND fiscal_year = 2025
                """, (city, state))
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


# ============================================================================
# ENDPOINTS
# ============================================================================

@app.get("/")
def read_root():
    """Health check"""
    return {
        "status": "healthy",
        "service": "ThrivingCare API",
        "version": "2.1 - AI Vetting",
        "ai_enabled": anthropic_client is not None,
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
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS available_date TEXT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS min_weekly_pay DECIMAL(10,2)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS open_to_travel BOOLEAN",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS ai_vetting_status VARCHAR(50) DEFAULT 'pending'",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS ai_vetting_score INTEGER",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS vetting_step INTEGER DEFAULT 0",
        "ALTER TABLE candidates ALTER COLUMN home_state TYPE VARCHAR(255)",
        "ALTER TABLE candidates ALTER COLUMN home_city TYPE VARCHAR(255)",
        "ALTER TABLE candidates ALTER COLUMN home_address TYPE TEXT",
        
        # APPLICATIONS TABLE (NEW)
        """CREATE TABLE IF NOT EXISTS applications (
            id SERIAL PRIMARY KEY,
            candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
            job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
            status VARCHAR(50) DEFAULT 'new',
            vetting_status VARCHAR(50) DEFAULT 'pending',
            vetting_step INTEGER DEFAULT 0,
            vetting_answers JSONB DEFAULT '{}',
            source VARCHAR(100) DEFAULT 'website',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        
        # APPLICATIONS TABLE - Add missing columns if table already exists
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'new'",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS vetting_status VARCHAR(50) DEFAULT 'pending'",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS vetting_step INTEGER DEFAULT 0",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS vetting_answers JSONB DEFAULT '{}'",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS source VARCHAR(100) DEFAULT 'website'",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS notes TEXT",
        "ALTER TABLE applications ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        
        # GSA RATES TABLE
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
        
        # PIPELINE STAGES TABLE
        """CREATE TABLE IF NOT EXISTS pipeline_stages (
            id SERIAL PRIMARY KEY,
            candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
            job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
            stage VARCHAR(50) NOT NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        
        # AI VETTING LOGS TABLE
        """CREATE TABLE IF NOT EXISTS ai_vetting_logs (
            id SERIAL PRIMARY KEY,
            candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
            application_id INTEGER REFERENCES applications(id) ON DELETE CASCADE,
            question_id VARCHAR(50),
            question TEXT,
            response TEXT,
            step INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        
        # CHAT STATE TABLE
        """CREATE TABLE IF NOT EXISTS candidate_chat_state (
            id SERIAL PRIMARY KEY,
            candidate_id INTEGER UNIQUE REFERENCES candidates(id) ON DELETE CASCADE,
            current_step VARCHAR(50) DEFAULT 'start',
            chat_history JSONB DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        
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


# ============================================================================
# JOBS ENDPOINTS
# ============================================================================

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
        return {"count": 0, "updated_at": datetime.now().isoformat()}


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
                query = "SELECT * FROM jobs WHERE active = TRUE AND enriched = TRUE"
                params = []
                
                if specialty:
                    query += " AND (specialty ILIKE %s OR title ILIKE %s OR discipline ILIKE %s)"
                    params.extend([f"%{specialty}%", f"%{specialty}%", f"%{specialty}%"])
                
                if location:
                    query += " AND (city ILIKE %s OR state ILIKE %s)"
                    params.extend([f"%{location}%", f"%{location}%"])
                
                query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
                params.extend([per_page, offset])
                
                cur.execute(query, params)
                jobs = cur.fetchall()
                
                # Get total count
                count_query = "SELECT COUNT(*) as count FROM jobs WHERE active = TRUE AND enriched = TRUE"
                cur.execute(count_query)
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
# CANDIDATE ENDPOINTS - WITH AI VETTING
# ============================================================================

@app.post("/api/candidates", response_model=CandidateResponse)
async def create_candidate(candidate: CandidateIntake):
    """
    Create new candidate from website signup.
    - Stores candidate info
    - Creates application record
    - Adds to pipeline
    - Immediately sends first vetting question via SMS
    """
    
    try:
        address_parts = parse_address(candidate.homeAddress)
        
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # 1. Insert candidate
                cur.execute("""
                    INSERT INTO candidates (
                        first_name, last_name, email, phone,
                        home_address, home_city, home_state, home_zip,
                        license_type, specialties, 
                        active, vetting_step, ai_vetting_status, created_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, ARRAY[%s],
                        TRUE, 1, 'in_progress', NOW()
                    ) RETURNING id
                """, (
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
                
                # 2. Create application record
                cur.execute("""
                    INSERT INTO applications (
                        candidate_id, status, vetting_status, vetting_step, source, created_at
                    ) VALUES (
                        %s, 'new', 'in_progress', 1, 'website', NOW()
                    ) RETURNING id
                """, (candidate_id,))
                
                application_id = cur.fetchone()['id']
                
                # 3. Add to pipeline
                cur.execute("""
                    INSERT INTO pipeline_stages (candidate_id, stage, notes, created_at)
                    VALUES (%s, 'new_application', 'Applied via website - AI vetting started', NOW())
                """, (candidate_id,))
                
                conn.commit()
        
        # 4. Send welcome + first vetting question via SMS
        if twilio_client:
            try:
                first_question = VETTING_QUESTIONS[0]
                welcome_message = f"""Hi {candidate.firstName}! 🎉 Welcome to ThrivingCare Staffing!

Thanks for applying. Let me ask a few quick questions to match you with the best opportunities.

{first_question['question']}"""
                
                twilio_client.messages.create(
                    body=welcome_message, 
                    from_=TWILIO_PHONE, 
                    to=candidate.phone
                )
                print(f"  ✓ Vetting SMS sent to {candidate.phone}")
            except Exception as e:
                print(f"Failed to send vetting SMS: {e}")
        
        print(f"✓ New application: {candidate.firstName} {candidate.lastName} ({candidate.email})")
        print(f"  Application ID: {application_id}, Candidate ID: {candidate_id}")
        
        return CandidateResponse(
            id=candidate_id,
            message="Welcome! Please check your phone for a text from us.",
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
        
        # Upload to S3 or save locally
        if s3_client:
            s3_client.put_object(
                Bucket=AWS_BUCKET,
                Key=s3_key,
                Body=resume_content,
                ContentType=resume.content_type
            )
            resume_url = f"https://{AWS_BUCKET}.s3.amazonaws.com/{s3_key}"
        else:
            resume_url = None
            print("  ⚠ No S3 configured - resume file not stored")
        
        # Update database
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if resume_url:
                    cur.execute(
                        "UPDATE candidates SET resume_url = %s, updated_at = NOW() WHERE id = %s",
                        (resume_url, candidate_id)
                    )
                    conn.commit()
                
                # Parse resume if PDF
                if file_extension.lower() == 'pdf':
                    try:
                        import pdfplumber
                        import io
                        pdf_file = io.BytesIO(resume_content)
                        text = ""
                        with pdfplumber.open(pdf_file) as pdf:
                            for page in pdf.pages:
                                text += page.extract_text() or ""
                        
                        extracted = parse_resume_text(text)
                        print(f"  📄 Resume parsed: {extracted}")
                        
                        # Update candidate with extracted data
                        updates = []
                        values = []
                        
                        if extracted.get('license_states'):
                            updates.append("license_states = %s")
                            values.append(','.join(extracted['license_states']))
                        
                        if extracted.get('certifications'):
                            updates.append("certifications = %s")
                            values.append(','.join(extracted['certifications']))
                        
                        if extracted.get('years_experience'):
                            updates.append("years_experience = %s")
                            values.append(extracted['years_experience'])
                        
                        if extracted.get('specialties'):
                            updates.append("specialty = %s")
                            values.append(','.join(extracted['specialties']))
                        
                        if extracted.get('licenses'):
                            updates.append("license_type = %s")
                            values.append(','.join(extracted['licenses']))
                        
                        if updates:
                            values.append(candidate_id)
                            cur.execute(f"UPDATE candidates SET {', '.join(updates)} WHERE id = %s", tuple(values))
                            conn.commit()
                            
                    except Exception as e:
                        print(f"  ⚠ Resume parsing failed: {e}")

        print(f"✓ Resume uploaded for candidate {candidate_id}")
        
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
# SMS WEBHOOK - AI VETTING + ENGAGEMENT
# ============================================================================

@app.post("/api/sms/webhook")
async def handle_incoming_sms(request: Request):
    """
    Twilio webhook for incoming SMS messages.
    Handles AI vetting flow + general questions.
    """
    
    try:
        form_data = await request.form()
        from_number = form_data.get('From', '')
        message_body = form_data.get('Body', '').strip()
        
        print(f"📱 Incoming SMS from {from_number}: {message_body}")
        
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Find candidate by phone
                cur.execute("SELECT * FROM candidates WHERE phone = %s", (from_number,))
                candidate = cur.fetchone()
                
                if not candidate:
                    print(f"  Unknown sender: {from_number}")
                    return Response(content="", media_type="text/xml")
                
                candidate_id = candidate['id']
                first_name = candidate['first_name']
                message_upper = message_body.upper().strip()
                response_msg = None
                
                # ============================================================
                # PRIORITY 1: Handle STOP/unsubscribe
                # ============================================================
                if message_upper in ['STOP', 'UNSUBSCRIBE', 'QUIT']:
                    cur.execute("UPDATE candidates SET active = FALSE WHERE id = %s", (candidate_id,))
                    conn.commit()
                    if twilio_client:
                        twilio_client.messages.create(
                            body="You've been unsubscribed from ThrivingCare. Reply START to re-subscribe.",
                            from_=TWILIO_PHONE,
                            to=from_number
                        )
                    return Response(content="<?xml version='1.0' encoding='UTF-8'?><Response></Response>", media_type="text/xml")
                
                # ============================================================
                # PRIORITY 2: Handle START/resubscribe
                # ============================================================
                elif message_upper in ['START', 'SUBSCRIBE']:
                    cur.execute("UPDATE candidates SET active = TRUE WHERE id = %s", (candidate_id,))
                    conn.commit()
                    response_msg = f"""Welcome back, {first_name}! 🎉

You're now resubscribed to ThrivingCare job alerts.

Browse jobs: https://thrivingcarestaffing.com/jobs"""
                
                # ============================================================
                # PRIORITY 3: Check if in VETTING FLOW
                # ============================================================
                elif candidate.get('ai_vetting_status') == 'in_progress':
                    current_step = candidate.get('vetting_step', 1)
                    
                    if current_step <= len(VETTING_QUESTIONS):
                        current_question = VETTING_QUESTIONS[current_step - 1]
                        field_name = current_question['field']
                        
                        # --------------------------------------------------------
                        # SMART DETECTION: Is this a question or an answer?
                        # --------------------------------------------------------
                        is_question = (
                            '?' in message_body or
                            message_body.lower().startswith(('what', 'where', 'when', 'how', 'why', 'which', 'can', 'do', 'does', 'is', 'are', 'will', 'would', 'could', 'should', 'tell me', 'explain'))
                        )
                        
                        # Also check for question-like phrases
                        question_phrases = ['jobs', 'positions', 'openings', 'pay', 'salary', 'housing', 'stipend', 'benefits', 'location', 'contract', 'start date', 'requirements']
                        mentions_job_topic = any(phrase in message_body.lower() for phrase in question_phrases)
                        
                        # If it looks like a question about jobs, answer it with AI
                        if is_question or (mentions_job_topic and len(message_body) > 15):
                            print(f"  📝 Detected question during vetting: {message_body}")
                            
                            # Get matching jobs for AI context
                            discipline = candidate.get('license_type') or candidate.get('discipline') or ''
                            cur.execute("""
                                SELECT * FROM jobs 
                                WHERE active = TRUE AND enriched = TRUE
                                AND (discipline ILIKE %s OR %s = '')
                                ORDER BY created_at DESC
                                LIMIT 5
                            """, (f"%{discipline}%", discipline))
                            matching_jobs = cur.fetchall()
                            
                            if not matching_jobs:
                                cur.execute("""
                                    SELECT * FROM jobs 
                                    WHERE active = TRUE AND enriched = TRUE
                                    ORDER BY created_at DESC
                                    LIMIT 5
                                """)
                                matching_jobs = cur.fetchall()
                            
                            # Generate AI response
                            ai_answer = None
                            if anthropic_client:
                                ai_answer = generate_ai_response(
                                    candidate=dict(candidate),
                                    message=message_body,
                                    jobs=[dict(j) for j in matching_jobs] if matching_jobs else []
                                )
                            
                            if ai_answer:
                                # Append the current vetting question to the AI response
                                response_msg = f"""{ai_answer}

---
To continue with your profile: {current_question['question']}"""
                            else:
                                # Fallback if AI fails
                                response_msg = f"""Great question, {first_name}! A recruiter will follow up with details.

In the meantime, let's finish your profile: {current_question['question']}"""
                        
                        else:
                            # --------------------------------------------------------
                            # This is a vetting answer - process it
                            # --------------------------------------------------------
                            
                            # Store the answer based on field type
                            if field_name == 'license_states':
                                # Extract state codes from response
                                states = re.findall(r'\b([A-Z]{2})\b', message_body.upper())
                                answer_value = ','.join(states) if states else message_body
                                cur.execute("UPDATE candidates SET license_states = %s WHERE id = %s", (answer_value, candidate_id))
                            
                            elif field_name == 'years_experience':
                                # Extract number
                                numbers = re.findall(r'\d+', message_body)
                                years = int(numbers[0]) if numbers else None
                                if years:
                                    cur.execute("UPDATE candidates SET years_experience = %s WHERE id = %s", (years, candidate_id))
                            
                            elif field_name == 'available_date':
                                cur.execute("UPDATE candidates SET available_date = %s WHERE id = %s", (message_body, candidate_id))
                            
                            elif field_name == 'min_weekly_pay':
                                # Extract number
                                numbers = re.findall(r'[\d,]+', message_body.replace(',', ''))
                                pay = int(numbers[0].replace(',', '')) if numbers else None
                                if pay:
                                    cur.execute("UPDATE candidates SET min_weekly_pay = %s WHERE id = %s", (pay, candidate_id))
                            
                            elif field_name == 'open_to_travel':
                                is_open = message_upper in ['YES', 'Y', 'YEAH', 'YEP', 'SURE', 'OK', 'OKAY']
                                cur.execute("UPDATE candidates SET open_to_travel = %s WHERE id = %s", (is_open, candidate_id))
                            
                            # Log the vetting response
                            cur.execute("""
                                INSERT INTO ai_vetting_logs (candidate_id, question_id, question, response, step, created_at)
                                VALUES (%s, %s, %s, %s, %s, NOW())
                            """, (candidate_id, current_question['id'], current_question['question'], message_body, current_step))
                            
                            # Also update application vetting_answers
                            cur.execute("""
                                UPDATE applications 
                                SET vetting_answers = vetting_answers || %s::jsonb,
                                    vetting_step = %s
                                WHERE candidate_id = %s
                            """, (json.dumps({current_question['id']: message_body}), current_step + 1, candidate_id))
                            
                            # Move to next question or complete
                            next_step = current_step + 1
                            
                            if next_step <= len(VETTING_QUESTIONS):
                                # Send next question
                                next_question = VETTING_QUESTIONS[next_step - 1]
                                cur.execute("UPDATE candidates SET vetting_step = %s WHERE id = %s", (next_step, candidate_id))
                                
                                response_msg = f"""Got it! ✅

{next_question['question']}"""
                            
                            else:
                                # Vetting complete!
                                cur.execute("""
                                    UPDATE candidates 
                                    SET vetting_step = %s, ai_vetting_status = 'completed' 
                                    WHERE id = %s
                                """, (next_step, candidate_id))
                                
                                cur.execute("""
                                    UPDATE applications 
                                    SET vetting_status = 'completed', status = 'vetted', updated_at = NOW()
                                    WHERE candidate_id = %s
                                """, (candidate_id,))
                                
                                cur.execute("""
                                    UPDATE pipeline_stages 
                                    SET stage = 'vetted', notes = 'AI vetting completed'
                                    WHERE candidate_id = %s AND stage = 'new_application'
                                """, (candidate_id,))
                            
                            response_msg = f"""Excellent, {first_name}! ✅🎉

Your profile is complete! We'll match you with opportunities that fit your preferences.

A recruiter will reach out soon with personalized job matches.

Browse all jobs: https://thrivingcarestaffing.com/jobs

Reply anytime with questions about specific positions!"""
                        
                        conn.commit()
                
                # ============================================================
                # PRIORITY 4: Handle YES/INTERESTED
                # ============================================================
                elif message_upper in ['YES', 'INTERESTED', 'Y']:
                    cur.execute("""
                        INSERT INTO pipeline_stages (candidate_id, stage, notes, created_at)
                        VALUES (%s, 'contacted', 'Expressed interest via SMS', NOW())
                    """, (candidate_id,))
                    conn.commit()
                    
                    response_msg = f"""Great choice, {first_name}! 🎉

A recruiter will reach out within 24 hours to discuss next steps.

View all jobs: https://thrivingcarestaffing.com/jobs"""
                
                # ============================================================
                # PRIORITY 5: Handle HELP
                # ============================================================
                elif message_upper == 'HELP':
                    response_msg = f"""Hi {first_name}! Here's how I can help:

📋 Ask me about any job details (pay, location, requirements)
💬 Reply YES to express interest in a job
🔍 Browse jobs: thrivingcarestaffing.com/jobs
📞 Need a human? Reply "CALL ME"

Reply STOP to unsubscribe."""
                
                # ============================================================
                # PRIORITY 6: Handle CALL ME / recruiter requests
                # ============================================================
                elif 'CALL ME' in message_upper or ('CALL' in message_upper and 'RECRUITER' in message_upper):
                    response_msg = f"""Got it, {first_name}! 📞

A recruiter will call you within 24 hours (M-F 9am-6pm EST).

Email: hello@thrivingcarestaffing.com

- ThrivingCare Team"""
                
                # ============================================================
                # PRIORITY 7: AI-POWERED RESPONSE for questions
                # ============================================================
                else:
                    # Get jobs matching this candidate's discipline
                    discipline = candidate.get('license_type') or candidate.get('discipline') or ''
                    cur.execute("""
                        SELECT * FROM jobs 
                        WHERE active = TRUE AND enriched = TRUE
                        AND (discipline ILIKE %s OR %s = '')
                        ORDER BY created_at DESC
                        LIMIT 5
                    """, (f"%{discipline}%", discipline))
                    matching_jobs = cur.fetchall()
                    
                    # If no discipline matches, get recent jobs
                    if not matching_jobs:
                        cur.execute("""
                            SELECT * FROM jobs 
                            WHERE active = TRUE AND enriched = TRUE
                            ORDER BY created_at DESC
                            LIMIT 5
                        """)
                        matching_jobs = cur.fetchall()
                    
                    # Try AI response
                    if anthropic_client:
                        response_msg = generate_ai_response(
                            candidate=dict(candidate),
                            message=message_body,
                            jobs=[dict(j) for j in matching_jobs] if matching_jobs else []
                        )
                    
                    # Fallback if AI fails or not configured
                    if not response_msg:
                        response_msg = get_fallback_response(message_body, first_name)
                
                # ============================================================
                # SEND RESPONSE
                # ============================================================
                if response_msg and twilio_client:
                    # Truncate if too long for SMS (1600 char limit)
                    if len(response_msg) > 1500:
                        response_msg = response_msg[:1450] + "...\n\nReply for more!"
                    
                    twilio_client.messages.create(
                        body=response_msg,
                        from_=TWILIO_PHONE,
                        to=from_number
                    )
                    print(f"  ✓ Sent response to {from_number}")
        
        return Response(content="<?xml version='1.0' encoding='UTF-8'?><Response></Response>", media_type="text/xml")
        
    except Exception as e:
        print(f"Error handling SMS webhook: {e}")
        import traceback
        traceback.print_exc()
        return Response(content="", media_type="text/xml")


# ============================================================================
# ADMIN: APPLICATIONS
# ============================================================================

@app.get("/api/admin/applications")
async def get_applications(
    status: Optional[str] = None,
    x_admin_password: str = Header(None)
):
    """Get all applications"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = """
                    SELECT 
                        a.*,
                        c.first_name, c.last_name, c.email, c.phone,
                        c.license_type, c.license_states, c.years_experience,
                        c.available_date, c.min_weekly_pay, c.open_to_travel,
                        c.home_city, c.home_state
                    FROM applications a
                    JOIN candidates c ON a.candidate_id = c.id
                    WHERE 1=1
                """
                params = []
                
                if status:
                    query += " AND a.status = %s"
                    params.append(status)
                
                query += " ORDER BY a.created_at DESC"
                
                cur.execute(query, params)
                applications = cur.fetchall()
                
                return {"applications": applications}
                
    except Exception as e:
        print(f"Error fetching applications: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/admin/applications/{application_id}/status")
async def update_application_status(
    application_id: int,
    status: str,
    x_admin_password: str = Header(None)
):
    """Update application status"""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE applications 
                    SET status = %s, updated_at = NOW() 
                    WHERE id = %s
                """, (status, application_id))
                conn.commit()
                return {"success": True, "message": f"Application status updated to {status}"}
    except Exception as e:
        print(f"Error updating application: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# ADMIN: JOB CREATION
# ============================================================================

@app.post("/api/admin/jobs")
async def create_job_admin(
    job: AdminJobCreate,
    x_admin_password: str = Header(None)
):
    """Create a new job listing with auto-calculated pay package."""
    
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid admin password")
    
    try:
        candidate_hourly = job.bill_rate * (1 - job.margin_percent / 100)
        weekly_gross = candidate_hourly * job.hours_per_week
        contract_total = weekly_gross * job.duration_weeks
        location = f"{job.city}, {job.state}"
        
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
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
                """, (
                    job.title, job.discipline, job.facility, job.setting, job.city, job.state, location,
                    job.duration_weeks, job.hours_per_week, job.shift, job.start_date if job.start_date else None,
                    job.bill_rate, job.margin_percent, round(candidate_hourly, 2), round(weekly_gross, 2), round(contract_total, 2),
                    job.description, job.requirements, job.benefits
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
                
                # Applications stats
                cur.execute("SELECT COUNT(*) as count FROM applications WHERE status = 'new'")
                new_applications = cur.fetchone()['count']
                
                cur.execute("SELECT COUNT(*) as count FROM applications WHERE vetting_status = 'completed'")
                vetted_applications = cur.fetchone()['count']
                
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
                    "new_applications": new_applications,
                    "vetted_applications": vetted_applications,
                    "by_discipline": by_discipline
                }
    except Exception as e:
        print(f"Error fetching analytics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# ADMIN: JOB MANAGEMENT
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
                    
                    job_discipline = (job.get('discipline') or '').lower()
                    candidate_discipline = (candidate.get('license_type') or candidate.get('discipline') or '').lower()
                    if job_discipline and candidate_discipline:
                        if job_discipline in candidate_discipline or candidate_discipline in job_discipline:
                            score += 50
                    
                    job_state = (job.get('state') or '').upper()
                    candidate_state = (candidate.get('home_state') or '').upper()
                    if job_state and candidate_state and job_state == candidate_state:
                        score += 30
                    
                    if candidate.get('resume_url'):
                        score += 10
                    
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
                    
                    job_discipline = (job.get('discipline') or '').lower()
                    candidate_discipline = (candidate.get('license_type') or candidate.get('discipline') or '').lower()
                    if job_discipline and candidate_discipline:
                        if job_discipline in candidate_discipline or candidate_discipline in job_discipline:
                            score += 50
                    
                    job_state = (job.get('state') or '').upper()
                    candidate_state = (candidate.get('home_state') or '').upper()
                    if job_state and candidate_state and job_state == candidate_state:
                        score += 30
                    
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
# GSA RATES ENDPOINTS
# ============================================================================

@app.get("/api/gsa-rates")
def get_gsa_rates_endpoint(city: str, state: str):
    """Get GSA per diem rates for a specific location"""
    rates = get_gsa_rates_internal(city, state)
    location_key = f"{city}, {state}"
    is_standard = location_key not in GSA_RATES_FY2025
    
    return {
        "location": location_key,
        "daily_lodging": rates["lodging"],
        "daily_mie": rates["mie"],
        "monthly_lodging": rates["lodging"] * 30,
        "monthly_mie": rates["mie"] * 21.65,
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
    """One-time endpoint to populate GSA rates into database."""
    try:
        inserted = 0
        
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                for location, rates in GSA_RATES_FY2025.items():
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
            "fiscal_year": 2025
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to seed GSA rates: {str(e)}")


# ============================================================================
# PAY CALCULATOR
# ============================================================================

@app.post("/api/calculate-pay")
def calculate_pay_package(request: PayCalculatorRequest):
    """Calculate complete pay package from bill rate."""
    
    gsa_rates = get_gsa_rates_internal(request.city, request.state)
    daily_lodging = gsa_rates["lodging"]
    daily_mie = gsa_rates["mie"]
    
    weekly_revenue = request.bill_rate * request.hours_per_week
    weekly_gross_margin = weekly_revenue * request.gross_margin_pct
    after_margin = weekly_revenue - weekly_gross_margin
    weekly_burden = after_margin * request.burden_pct
    total_available = after_margin - weekly_burden
    
    if request.is_travel_contract:
        max_weekly_housing = daily_lodging * 7
        max_weekly_mie = daily_mie * 5
        max_weekly_stipends = max_weekly_housing + max_weekly_mie
        
        weekly_taxable_pay = total_available - max_weekly_stipends
        taxable_hourly = weekly_taxable_pay / request.hours_per_week
        
        min_hourly = 15.00
        
        if taxable_hourly < min_hourly:
            weekly_taxable_pay = min_hourly * request.hours_per_week
            remaining_for_stipends = total_available - weekly_taxable_pay
            
            if remaining_for_stipends > 0:
                stipend_ratio = remaining_for_stipends / max_weekly_stipends
                weekly_housing = max_weekly_housing * stipend_ratio
                weekly_mie = max_weekly_mie * stipend_ratio
            else:
                weekly_housing = 0
                weekly_mie = 0
            
            taxable_hourly = min_hourly
        else:
            weekly_housing = max_weekly_housing
            weekly_mie = max_weekly_mie
        
        total_weekly_pay = weekly_taxable_pay + weekly_housing + weekly_mie
        effective_hourly = total_weekly_pay / request.hours_per_week
        
        return {
            "contract_type": "Travel",
            "location": f"{request.city}, {request.state}",
            "bill_rate": round(request.bill_rate, 2),
            "hours_per_week": request.hours_per_week,
            "gsa_daily_lodging": daily_lodging,
            "gsa_daily_mie": daily_mie,
            "gsa_max_weekly_housing": round(max_weekly_housing, 2),
            "gsa_max_weekly_mie": round(max_weekly_mie, 2),
            "weekly_taxable_pay": round(weekly_taxable_pay, 2),
            "weekly_housing_stipend": round(weekly_housing, 2),
            "weekly_mie_stipend": round(weekly_mie, 2),
            "total_weekly_pay": round(total_weekly_pay, 2),
            "taxable_hourly_rate": round(taxable_hourly, 2),
            "effective_hourly_rate": round(effective_hourly, 2),
            "weekly_gross_margin": round(weekly_gross_margin, 2),
            "weekly_burden": round(weekly_burden, 2),
            "weekly_revenue": round(weekly_revenue, 2),
        }
    
    else:
        taxable_hourly = total_available / request.hours_per_week
        total_weekly_pay = total_available
        
        return {
            "contract_type": "Local",
            "location": f"{request.city}, {request.state}",
            "bill_rate": round(request.bill_rate, 2),
            "hours_per_week": request.hours_per_week,
            "weekly_taxable_pay": round(total_weekly_pay, 2),
            "weekly_housing_stipend": 0,
            "weekly_mie_stipend": 0,
            "total_weekly_pay": round(total_weekly_pay, 2),
            "taxable_hourly_rate": round(taxable_hourly, 2),
            "effective_hourly_rate": round(taxable_hourly, 2),
            "weekly_gross_margin": round(weekly_gross_margin, 2),
            "weekly_burden": round(weekly_burden, 2),
            "weekly_revenue": round(weekly_revenue, 2),
        }


# ============================================================================
# CHAT ENDPOINT
# ============================================================================

@app.post("/api/chat")
async def chat_with_candidate(chat: ChatMessage):
    """AI chat endpoint for candidate engagement"""
    
    candidate_data = None
    chat_state = None
    
    if chat.candidate_id:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM candidates WHERE id = %s", (chat.candidate_id,))
                candidate_data = cur.fetchone()
                
                cur.execute("SELECT * FROM candidate_chat_state WHERE candidate_id = %s", (chat.candidate_id,))
                chat_state = cur.fetchone()
    
    if not chat_state:
        first_name = candidate_data.get('first_name', 'there') if candidate_data else 'there'
        
        if candidate_data and candidate_data.get('resume_url'):
            response = f"Welcome {first_name}! 👋\n\nI found info from your resume:\n📋 Specialty: {candidate_data.get('specialty', 'Not detected')}\n📍 Location: {candidate_data.get('home_city', 'Not detected')}\n\nIs this correct? Reply YES or NO."
            next_step = "confirm_resume"
        else:
            response = f"Welcome {first_name}! 👋\n\nLet me ask a few quick questions to find you the best matches.\n\n{VETTING_QUESTIONS[0]['question']}"
            next_step = VETTING_QUESTIONS[0]['id']
        
        if chat.candidate_id:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO candidate_chat_state (candidate_id, current_step) VALUES (%s, %s) ON CONFLICT (candidate_id) DO UPDATE SET current_step = %s", (chat.candidate_id, next_step, next_step))
                    conn.commit()
        
        return {"response": response, "next_question": next_step, "profile_completion": 10}
    
    current_step = chat_state.get('current_step', '')
    msg = chat.message.strip().lower()
    
    if current_step == 'confirm_resume':
        if 'yes' in msg:
            response = "Great! ✅\n\n" + VETTING_QUESTIONS[2]['question']
            next_step = VETTING_QUESTIONS[2]['id']
        else:
            response = "No problem!\n\n" + VETTING_QUESTIONS[0]['question']
            next_step = VETTING_QUESTIONS[0]['id']
    else:
        current_idx = next((i for i, q in enumerate(VETTING_QUESTIONS) if q['id'] == current_step), 0)
        
        if current_idx < len(VETTING_QUESTIONS) - 1:
            next_q = VETTING_QUESTIONS[current_idx + 1]
            response = f"Got it! ✅\n\n{next_q['question']}"
            next_step = next_q['id']
        else:
            response = "Excellent! ✅\n\nYour profile is updated. We'll text you when we find matching positions!\n\nBrowse jobs: https://thrivingcarestaffing.com/jobs"
            next_step = 'complete'
    
    if chat.candidate_id:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE candidate_chat_state SET current_step = %s WHERE candidate_id = %s", (next_step, chat.candidate_id))
                conn.commit()
    
    return {"response": response, "next_question": next_step, "profile_completion": 25}


# ============================================================================
# RESUME PARSING ENDPOINT
# ============================================================================

@app.post("/api/parse-resume")
async def parse_resume(file: UploadFile = File(...)):
    """Parse uploaded resume and extract information"""
    import pdfplumber
    import io
    
    if not file.filename.endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")
    
    try:
        contents = await file.read()
        pdf_file = io.BytesIO(contents)
        
        text = ""
        with pdfplumber.open(pdf_file) as pdf:
            for page in pdf.pages:
                text += page.extract_text() or ""
        
        extracted = parse_resume_text(text)
        
        return {
            "status": "success",
            "extracted": extracted,
            "raw_text_preview": text[:500] + "..." if len(text) > 500 else text
        }
    except Exception as e:
        print(f"Error parsing resume: {e}")
        raise HTTPException(status_code=500, detail=f"Error parsing resume: {str(e)}")


# ============================================================================
# RUN SERVER
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
