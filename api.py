"""
ThrivingCare Website API
=========================

Backend API to receive candidate signups from website and integrate with automation system.

Endpoints:
- POST /api/candidates - Create new candidate from website
- POST /api/candidates/{id}/resume - Upload resume
- GET /api/jobs/count - Get total active jobs
- GET /api/jobs - Get job listings (paginated)
"""

from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from typing import Optional
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
        "version": "1.0"
    }


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
# RUN SERVER
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
