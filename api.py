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

from fastapi import FastAPI, File, UploadFile, HTTPException, Form
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

# Standard CONUS rate for unlisted locations
STANDARD_CONUS = {"lodging": 107, "mie": 68}


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


@app.post("/api/calculate-pay")
def calculate_pay_package(request: PayCalculatorRequest):
    """
    Calculate complete pay package from bill rate.
    
    Uses GSA per diem rates to split compensation into:
    - Taxable hourly wage
    - Tax-free housing stipend (travel contracts only)
    - Tax-free M&IE stipend (travel contracts only)
    """
    
    # Get GSA rates
    gsa_rates = get_gsa_rates(request.city, request.state)
    daily_lodging = gsa_rates["lodging"]
    daily_mie = gsa_rates["mie"]
    
    # Calculate weekly/monthly values
    weeks_per_month = 4.33
    days_per_week = 5
    hours_per_month = request.hours_per_week * weeks_per_month
    
    # Monthly stipend calculations
    monthly_lodging_stipend = daily_lodging * 30
    monthly_mie_stipend = daily_mie * days_per_week * weeks_per_month
    
    # Gross calculations
    gross_monthly_revenue = request.bill_rate * hours_per_month
    
    # Your margin
    gross_margin_amount = gross_monthly_revenue * request.gross_margin_pct
    after_margin = gross_monthly_revenue - gross_margin_amount
    
    # Employer burden
    burden_amount = after_margin * request.burden_pct
    total_available_for_pay = after_margin - burden_amount
    
    if request.is_travel_contract:
        # Travel contract: Split into taxable + stipends
        total_monthly_stipends = monthly_lodging_stipend + monthly_mie_stipend
        taxable_monthly = total_available_for_pay - total_monthly_stipends
        
        # Minimum wage check
        min_hourly = 15.00
        min_taxable_monthly = min_hourly * hours_per_month
        
        if taxable_monthly < min_taxable_monthly:
            taxable_monthly = min_taxable_monthly
            total_monthly_stipends = total_available_for_pay - taxable_monthly
            stipend_ratio = max(0, total_monthly_stipends / (monthly_lodging_stipend + monthly_mie_stipend))
            monthly_lodging_stipend = monthly_lodging_stipend * stipend_ratio
            monthly_mie_stipend = monthly_mie_stipend * stipend_ratio
        
        taxable_hourly = taxable_monthly / hours_per_month
        total_monthly_value = taxable_monthly + monthly_lodging_stipend + monthly_mie_stipend
        effective_hourly = total_monthly_value / hours_per_month
        
        return {
            "contract_type": "Travel",
            "location": f"{request.city}, {request.state}",
            "bill_rate": round(request.bill_rate, 2),
            "gross_margin_pct": request.gross_margin_pct,
            "burden_pct": request.burden_pct,
            "hours_per_week": request.hours_per_week,
            
            "gsa_daily_lodging": daily_lodging,
            "gsa_daily_mie": daily_mie,
            
            "taxable_hourly_rate": round(taxable_hourly, 2),
            "monthly_housing_stipend": round(monthly_lodging_stipend, 2),
            "monthly_mie_stipend": round(monthly_mie_stipend, 2),
            "weekly_housing_stipend": round(monthly_lodging_stipend / weeks_per_month, 2),
            "weekly_mie_stipend": round(monthly_mie_stipend / weeks_per_month, 2),
            
            "effective_hourly_rate": round(effective_hourly, 2),
            "total_weekly_pay": round(total_monthly_value / weeks_per_month, 2),
            "total_monthly_pay": round(total_monthly_value, 2),
            
            "gross_margin_monthly": round(gross_margin_amount, 2),
            "burden_monthly": round(burden_amount, 2),
        }
    
    else:
        # Local contract: Fully taxable
        taxable_hourly = total_available_for_pay / hours_per_month
        
        return {
            "contract_type": "Local",
            "location": f"{request.city}, {request.state}",
            "bill_rate": round(request.bill_rate, 2),
            "gross_margin_pct": request.gross_margin_pct,
            "burden_pct": request.burden_pct,
            "hours_per_week": request.hours_per_week,
            
            "gsa_daily_lodging": daily_lodging,
            "gsa_daily_mie": daily_mie,
            
            "taxable_hourly_rate": round(taxable_hourly, 2),
            "monthly_housing_stipend": 0,
            "monthly_mie_stipend": 0,
            "weekly_housing_stipend": 0,
            "weekly_mie_stipend": 0,
            
            "effective_hourly_rate": round(taxable_hourly, 2),
            "total_weekly_pay": round(taxable_hourly * request.hours_per_week, 2),
            "total_monthly_pay": round(total_available_for_pay, 2),
            
            "gross_margin_monthly": round(gross_margin_amount, 2),
            "burden_monthly": round(burden_amount, 2),
        }


# ============================================================================
# RUN SERVER
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)

