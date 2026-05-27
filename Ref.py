"""
Kisumu County Hospital Referral System
Streamlit UI + Django ORM (replaces SQLAlchemy)
"""

# ── Django bootstrap (must happen before any model imports) ─────────────────
import os
import sys
import django

# Point Django at our settings module
SETTINGS_MODULE = 'referral_system.settings'
os.environ.setdefault('DJANGO_SETTINGS_MODULE', SETTINGS_MODULE)

# Add the project root to sys.path so Django can find the app
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Configure Django once per process
if not django.conf.settings.configured:
    django.setup()
# ─────────────────────────────────────────────────────────────────────────────

import streamlit as st
import hashlib
import io
import json
import math
import secrets
import threading
import time
from datetime import datetime

import pandas as pd

# Django ORM models
from referral_system.models import (
    Patient, Ambulance, Referral, HandoverForm, Communication,
    LocationUpdate, BedCapacity, SHAMember, SHAClaim, AuditLog, OfflineQueue,
)

# Optional dependencies
try:
    import plotly.express as px
    PLOTLY_AVAILABLE = True
except ImportError:
    PLOTLY_AVAILABLE = False

try:
    import pydeck as pdk
    PYDECK_AVAILABLE = True
except ImportError:
    PYDECK_AVAILABLE = False

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

try:
    import qrcode
    QRCODE_AVAILABLE = True
except ImportError:
    QRCODE_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# =============================================================================
# CONFIGURATION
# =============================================================================
class Config:
    SMTP_SERVER = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
    SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
    SMTP_USERNAME = os.getenv('SMTP_USERNAME')
    SMTP_PASSWORD = os.getenv('SMTP_PASSWORD')
    DEFAULT_LATITUDE = -0.0916
    DEFAULT_LONGITUDE = 34.7680
    DEFAULT_ZOOM = 10
    PAGE_TITLE = "Kisumu County Hospital Referral System"
    PAGE_ICON = "🏥"
    LAYOUT = "wide"
    GOOGLE_MAPS_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY', '')
    NOTIFICATION_CHECK_INTERVAL = 30
    LOCATION_UPDATE_INTERVAL = 10
    SHA_BASE_CHARGE_KES = 4500.0
    SHA_BASE_DISTANCE_KM = 25.0
    SHA_PER_KM_CHARGE_KES = 75.0


# =============================================================================
# DATABASE SERVICE  (pure Django ORM — no SQLAlchemy)
# =============================================================================
class Database:
    """Thin service layer wrapping Django ORM queries."""

    # ── Migrations / table creation ─────────────────────────────────────────
    @staticmethod
    def run_migrations():
        """Create/update database tables via Django's migration framework."""
        from django.core.management import call_command
        try:
            call_command('migrate', '--run-syncdb', verbosity=0, interactive=False)
        except Exception as e:
            # Tables may already exist — silently continue
            pass

    # ── Patients ────────────────────────────────────────────────────────────
    def add_patient(self, patient_data: dict) -> Patient:
        if 'patient_id' not in patient_data or not patient_data['patient_id']:
            patient_data['patient_id'] = f"PAT{secrets.token_hex(4).upper()}"
        patient = Patient(**patient_data)
        patient.save()
        return patient

    def get_patient_by_id(self, patient_id: str):
        return Patient.objects.filter(patient_id=patient_id).first()

    def get_all_patients(self):
        return list(Patient.objects.all())

    # ── Ambulances ──────────────────────────────────────────────────────────
    def get_available_ambulances(self):
        return list(Ambulance.objects.filter(status='Available'))

    def get_all_ambulances(self):
        return list(Ambulance.objects.all())

    def update_ambulance_status(self, ambulance_id: str, status: str, patient_id=None):
        Ambulance.objects.filter(ambulance_id=ambulance_id).update(
            status=status,
            **({'current_patient': patient_id} if patient_id else {})
        )

    def find_nearest_ambulance(self, hospital_lat, hospital_lng, min_fuel_level=20.0, require_als=False):
        available = self.get_available_ambulances()
        nearest, min_dist = None, float('inf')
        for amb in available:
            if amb.fuel_level < min_fuel_level:
                continue
            if amb.latitude is not None and amb.longitude is not None:
                dist = self.calculate_distance(hospital_lat, hospital_lng, amb.latitude, amb.longitude)
                if dist < min_dist:
                    min_dist, nearest = dist, amb
        return nearest

    def update_ambulance_fuel(self, ambulance_id: str, distance_km=None, new_fuel_level=None):
        amb = Ambulance.objects.filter(ambulance_id=ambulance_id).first()
        if not amb:
            return None
        if distance_km is not None:
            fuel_used = distance_km * amb.fuel_consumption_rate
            amb.fuel_level = max(0.0, amb.fuel_level - fuel_used)
        elif new_fuel_level is not None:
            amb.fuel_level = max(0.0, min(100.0, new_fuel_level))
        amb.save(update_fields=['fuel_level'])
        return amb.fuel_level

    # ── Referrals ────────────────────────────────────────────────────────────
    def add_referral(self, data: dict) -> Referral:
        ref = Referral(**data)
        ref.save()
        return ref

    # ── Handover forms ───────────────────────────────────────────────────────
    def add_handover_form(self, data: dict) -> HandoverForm:
        hf = HandoverForm(**data)
        hf.save()
        return hf

    # ── Communications ───────────────────────────────────────────────────────
    def add_communication(self, data: dict) -> Communication:
        c = Communication(**data)
        c.save()
        return c

    def get_communications_for_patient(self, patient_id: str):
        return list(Communication.objects.filter(patient_id=patient_id).order_by('-timestamp'))

    def get_communications_for_ambulance(self, ambulance_id: str):
        return list(Communication.objects.filter(ambulance_id=ambulance_id).order_by('-timestamp'))

    # ── Location updates ─────────────────────────────────────────────────────
    def add_location_update(self, data: dict) -> LocationUpdate:
        lu = LocationUpdate(**data)
        lu.save()
        return lu

    def get_latest_location(self, ambulance_id: str):
        return LocationUpdate.objects.filter(ambulance_id=ambulance_id).order_by('-timestamp').first()

    # ── Bed capacity ─────────────────────────────────────────────────────────
    def get_bed_capacity(self, hospital_name: str):
        return BedCapacity.objects.filter(hospital_name=hospital_name).first()

    def get_all_bed_capacities(self):
        return list(BedCapacity.objects.all())

    def update_bed_capacity(self, hospital_name: str, data: dict, updated_by='system'):
        cap, _ = BedCapacity.objects.get_or_create(hospital_name=hospital_name)
        for k, v in data.items():
            setattr(cap, k, v)
        cap.updated_by = updated_by
        cap.updated_at = datetime.utcnow()
        cap.save()
        return cap

    def get_capacity_status(self, hospital_name: str):
        cap = self.get_bed_capacity(hospital_name)
        if not cap or cap.total_beds == 0:
            return 'unknown', 0
        pct = (cap.occupied_beds / cap.total_beds) * 100
        if pct >= 90:
            return 'red', pct
        elif pct >= 70:
            return 'amber', pct
        return 'green', pct

    # ── SHA ──────────────────────────────────────────────────────────────────
    def verify_sha_member(self, identifier: str):
        return SHAMember.objects.filter(
            sha_member_number=identifier
        ).first() or SHAMember.objects.filter(national_id=identifier).first()

    def create_sha_claim(self, patient_id: str, ambulance_id: str, distance_km: float) -> SHAClaim:
        if distance_km <= Config.SHA_BASE_DISTANCE_KM:
            base_charge = Config.SHA_BASE_CHARGE_KES
            additional_charge = 0.0
        else:
            base_charge = Config.SHA_BASE_CHARGE_KES
            additional_charge = (distance_km - Config.SHA_BASE_DISTANCE_KM) * Config.SHA_PER_KM_CHARGE_KES
        total_amount = base_charge + additional_charge
        claim_id = f"SHA-{secrets.token_hex(5).upper()}"
        claim = SHAClaim(
            claim_id=claim_id,
            patient_id=patient_id,
            ambulance_id=ambulance_id,
            distance_km=round(distance_km, 2),
            base_charge=base_charge,
            additional_charge=round(additional_charge, 2),
            total_amount=round(total_amount, 2),
        )
        claim.save()
        Patient.objects.filter(patient_id=patient_id).update(
            sha_claim_id=claim_id,
            sha_billing_amount_kes=round(total_amount, 2),
            sha_distance_km=round(distance_km, 2),
            sha_claim_status='Submitted',
        )
        return claim

    def get_sha_claims(self):
        return list(SHAClaim.objects.order_by('-submitted_at'))

    # ── Audit log ────────────────────────────────────────────────────────────
    def log_action(self, user_id, user_role, action, resource_type, resource_id='', details=''):
        AuditLog(
            user_id=user_id, user_role=user_role, action=action,
            resource_type=resource_type, resource_id=str(resource_id),
            details=details, ip_address='N/A',
        ).save()

    def get_audit_logs(self, limit=100):
        return list(AuditLog.objects.order_by('-timestamp')[:limit])

    # ── Offline queue ────────────────────────────────────────────────────────
    def queue_offline_action(self, action_type: str, payload: dict):
        item = OfflineQueue(action_type=action_type, payload=payload)
        item.save()
        return item

    def get_pending_offline_actions(self):
        return list(OfflineQueue.objects.filter(synced=False))

    def mark_offline_synced(self, item_id: int, error=None):
        OfflineQueue.objects.filter(pk=item_id).update(
            synced=(error is None),
            synced_at=datetime.utcnow(),
            error_message=error or '',
        )

    # ── Utilities ────────────────────────────────────────────────────────────
    @staticmethod
    def calculate_distance(lat1, lon1, lat2, lon2) -> float:
        R = 6371
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
             math.sin(dlon / 2) ** 2)
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# =============================================================================
# AUTHENTICATION
# =============================================================================
class Authentication:
    def __init__(self):
        self.credentials = {
            'admin': {
                'password': self._hash('admin123'), 'email': 'admin@kisumu.gov',
                'role': 'Admin', 'hospital': 'All Facilities', 'name': 'System Administrator',
            },
            'hospital_staff': {
                'password': self._hash('staff123'), 'email': 'staff@joortrh.go.ke',
                'role': 'Hospital Staff',
                'hospital': 'Jaramogi Oginga Odinga Teaching & Referral Hospital (JOOTRH)',
                'name': 'Hospital Staff Member',
            },
            'driver': {
                'password': self._hash('driver123'), 'email': 'driver@kisumu.gov',
                'role': 'Ambulance Driver', 'hospital': 'Ambulance Service', 'name': 'Ambulance Driver',
            },
            'kisumu_staff': {
                'password': self._hash('kisumu123'), 'email': 'staff@kisumuhospital.go.ke',
                'role': 'Hospital Staff', 'hospital': 'Kisumu County Referral Hospital',
                'name': 'Kisumu County Hospital Staff',
            },
        }

    @staticmethod
    def _hash(password: str) -> str:
        return hashlib.sha256(password.encode()).hexdigest()

    def authenticate_user(self, username: str, password: str):
        user = self.credentials.get(username)
        if user and self._hash(password) == user['password']:
            return user
        return None

    def setup_auth_ui(self):
        st.sidebar.title("🔐 Login")
        username = st.sidebar.text_input("Username")
        password = st.sidebar.text_input("Password", type="password")
        if st.sidebar.button("Login", use_container_width=True):
            user = self.authenticate_user(username, password)
            if user:
                st.session_state.user = user
                st.session_state.authenticated = True
                st.sidebar.success(f"Welcome {user['role']}!")
                st.rerun()
            else:
                st.sidebar.error("Invalid credentials")
        if st.session_state.get('authenticated'):
            if st.sidebar.button("Logout", use_container_width=True):
                st.session_state.clear()
                st.rerun()

    def require_auth(self, roles=None):
        if not st.session_state.get('authenticated'):
            st.warning("Please login to access this page")
            return False
        if roles and st.session_state.user['role'] not in roles:
            st.error(f"Access denied. Required roles: {', '.join(roles)}")
            return False
        return True


# =============================================================================
# SHA BILLING SERVICE
# =============================================================================
class SHABillingService:
    def __init__(self, database: Database):
        self.database = database

    def verify_member(self, identifier: str):
        return self.database.verify_sha_member(identifier)

    def calculate_billing(self, distance_km: float) -> dict:
        if distance_km <= Config.SHA_BASE_DISTANCE_KM:
            base, extra = Config.SHA_BASE_CHARGE_KES, 0.0
        else:
            base = Config.SHA_BASE_CHARGE_KES
            extra = (distance_km - Config.SHA_BASE_DISTANCE_KM) * Config.SHA_PER_KM_CHARGE_KES
        return {
            'base_charge': base,
            'additional_charge': round(extra, 2),
            'total': round(base + extra, 2),
            'distance_km': round(distance_km, 2),
            'tariff_note': (
                f"KSh {Config.SHA_BASE_CHARGE_KES:,.0f} base "
                f"(0–{Config.SHA_BASE_DISTANCE_KM:.0f} km) + "
                f"KSh {Config.SHA_PER_KM_CHARGE_KES:.0f}/km thereafter"
            ),
        }

    def submit_claim(self, patient_id, ambulance_id, distance_km):
        return self.database.create_sha_claim(patient_id, ambulance_id, distance_km)

    def render_sha_panel(self, patient=None):
        st.subheader("🏛️ SHA / SHIF Insurance Integration")
        col1, col2 = st.columns(2)
        with col1:
            national_id = st.text_input("National ID / Huduma Namba", key="sha_national_id")
            sha_number = st.text_input("SHA Member Number (optional)", key="sha_member_num")
        with col2:
            if st.button("🔍 Verify SHA Membership", key="sha_verify_btn"):
                identifier = sha_number or national_id
                if identifier:
                    member = self.verify_member(identifier)
                    if member:
                        st.success(f"✅ SHA Member verified: {member.member_name} — {member.cover_type}")
                        st.session_state['sha_verified'] = True
                        st.session_state['sha_member'] = member
                    else:
                        st.warning("⚠️ Member not found in SHA registry. Patient may be uninsured.")
                        st.session_state['sha_verified'] = False
                        st.info("You may still create the referral. Mark patient as self-pay or county subsidised.")
                else:
                    st.error("Please enter National ID or SHA Member Number")

        if st.session_state.get('sha_verified'):
            m = st.session_state.get('sha_member')
            if m:
                st.markdown(
                    f'<div style="background:#E1F5EE;padding:10px 14px;border-radius:8px;margin-top:8px;">'
                    f'<b>✅ Verified Member</b><br>'
                    f'Name: {m.member_name} &nbsp;|&nbsp; Cover: {m.cover_type} &nbsp;|&nbsp; '
                    f'Active: {"Yes" if m.active else "No"}'
                    f'</div>', unsafe_allow_html=True,
                )
        return national_id, sha_number


# =============================================================================
# BED CAPACITY SERVICE
# =============================================================================
class BedCapacityService:
    def __init__(self, database: Database):
        self.database = database

    def get_capacity_badge(self, hospital_name: str):
        status, pct = self.database.get_capacity_status(hospital_name)
        badges = {
            'red': (f"🔴 {pct:.0f}% full", 'red'),
            'amber': (f"🟡 {pct:.0f}% full", 'amber'),
            'green': (f"🟢 {pct:.0f}% full", 'green'),
        }
        return badges.get(status, ("⚪ Unknown", "unknown"))

    def render_capacity_dashboard(self):
        st.subheader("🏥 Live Hospital Bed Capacity")
        capacities = self.database.get_all_bed_capacities()
        if not capacities:
            st.info("No bed capacity data loaded yet. Update via the Bed Management tab.")
            return
        data = []
        for c in capacities:
            occ_pct = (c.occupied_beds / c.total_beds * 100) if c.total_beds > 0 else 0
            status, _ = self.database.get_capacity_status(c.hospital_name)
            data.append({
                'Hospital': c.hospital_name,
                'Total Beds': c.total_beds,
                'Occupied': c.occupied_beds,
                'Available': c.total_beds - c.occupied_beds,
                'Occupancy %': round(occ_pct, 1),
                'ICU Available': c.icu_total - c.icu_occupied,
                'Status': status.upper(),
            })
        st.dataframe(pd.DataFrame(data), use_container_width=True)

    def render_capacity_update_form(self, database: Database, user: dict):
        st.subheader("📝 Update Hospital Bed Capacity")
        referral_hospitals = [
            'Jaramogi Oginga Odinga Teaching & Referral Hospital (JOOTRH)',
            'Kisumu County Referral Hospital',
        ]
        hospital = st.selectbox("Hospital", referral_hospitals, key="cap_hospital")
        existing = database.get_bed_capacity(hospital)
        col1, col2 = st.columns(2)
        with col1:
            total_beds = st.number_input("Total Beds", min_value=0, value=int(existing.total_beds) if existing else 100, key="cap_total")
            occupied = st.number_input("Occupied Beds", min_value=0, value=int(existing.occupied_beds) if existing else 70, key="cap_occ")
            icu_total = st.number_input("ICU Beds (Total)", min_value=0, value=int(existing.icu_total) if existing else 20, key="cap_icu_t")
            icu_occ = st.number_input("ICU Beds (Occupied)", min_value=0, value=int(existing.icu_occupied) if existing else 15, key="cap_icu_o")
        with col2:
            mat_total = st.number_input("Maternity Beds (Total)", min_value=0, value=int(existing.maternity_total) if existing else 30, key="cap_mat_t")
            mat_occ = st.number_input("Maternity (Occupied)", min_value=0, value=int(existing.maternity_occupied) if existing else 20, key="cap_mat_o")
            paed_total = st.number_input("Paediatric Beds (Total)", min_value=0, value=int(existing.paediatric_total) if existing else 25, key="cap_pae_t")
            paed_occ = st.number_input("Paediatric (Occupied)", min_value=0, value=int(existing.paediatric_occupied) if existing else 18, key="cap_pae_o")
        st.subheader("Specialist Availability")
        col3, col4 = st.columns(4)
        with col3:
            card_avail = st.checkbox("Cardiologist", value=existing.cardiologist_available if existing else False, key="sp_card")
        with col4:
            surg_avail = st.checkbox("Surgeon", value=existing.surgeon_available if existing else False, key="sp_surg")
        col5, col6 = st.columns(4)
        with col5:
            obs_avail = st.checkbox("Obstetrician", value=existing.obstetrician_available if existing else False, key="sp_obs")
        with col6:
            paed_avail = st.checkbox("Paediatrician", value=existing.paediatrician_available if existing else False, key="sp_paed")
        if st.button("💾 Save Capacity Update", use_container_width=True, key="save_cap"):
            data = {
                'total_beds': total_beds, 'occupied_beds': occupied,
                'icu_total': icu_total, 'icu_occupied': icu_occ,
                'maternity_total': mat_total, 'maternity_occupied': mat_occ,
                'paediatric_total': paed_total, 'paediatric_occupied': paed_occ,
                'cardiologist_available': card_avail, 'surgeon_available': surg_avail,
                'obstetrician_available': obs_avail, 'paediatrician_available': paed_avail,
            }
            database.update_bed_capacity(hospital, data, updated_by=user['role'])
            database.log_action(user['role'], user['role'], 'UPDATE_BED_CAPACITY', 'BedCapacity', hospital,
                                f"Occupancy: {occupied}/{total_beds}")
            st.success(f"✅ Capacity updated for {hospital}")
            st.rerun()


# =============================================================================
# FHIR SERVICE
# =============================================================================
class FHIRService:
    @staticmethod
    def build_patient_resource(patient: Patient) -> dict:
        return {
            "resourceType": "Patient",
            "id": patient.patient_id,
            "identifier": [
                {"system": "https://kisumu.go.ke/referral/patient-id", "value": patient.patient_id},
                {"system": "https://sha.go.ke/member", "value": patient.sha_member_number or ""},
            ],
            "name": [{"use": "official", "text": patient.name}],
            "birthDate": str(datetime.utcnow().year - patient.age) + "-01-01",
            "gender": "unknown",
            "generalPractitioner": [{"display": patient.referring_physician}],
        }

    @staticmethod
    def build_referral_resource(patient: Patient, ambulance=None) -> dict:
        priority = {'Red': 'urgent', 'Orange': 'asap'}.get(patient.triage_level, 'routine')
        resource = {
            "resourceType": "ServiceRequest",
            "id": patient.patient_id,
            "status": "active",
            "intent": "order",
            "priority": priority,
            "code": {"text": patient.condition},
            "subject": {"reference": f"Patient/{patient.patient_id}"},
            "requester": {"display": patient.referring_physician},
            "performer": [{"display": patient.receiving_hospital}],
            "locationReference": [{"display": patient.referring_hospital}],
            "note": [{"text": patient.notes or ""}],
            "authoredOn": (patient.referral_time.isoformat() if patient.referral_time else datetime.utcnow().isoformat()),
        }
        if ambulance:
            resource["extension"] = [{
                "url": "https://kisumu.go.ke/fhir/StructureDefinition/ambulance-id",
                "valueString": ambulance.ambulance_id,
            }]
        return resource

    @staticmethod
    def export_bundle(patient: Patient, ambulance=None) -> str:
        return json.dumps({
            "resourceType": "Bundle",
            "type": "transaction",
            "timestamp": datetime.utcnow().isoformat(),
            "entry": [
                {"resource": FHIRService.build_patient_resource(patient)},
                {"resource": FHIRService.build_referral_resource(patient, ambulance)},
            ],
        }, indent=2)

    @staticmethod
    def render_fhir_panel(patient: Patient, ambulance=None):
        st.subheader("🔗 FHIR R4 Interoperability")
        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("📄 Export FHIR Bundle", key=f"fhir_bundle_{patient.patient_id}", use_container_width=True):
                bundle_json = FHIRService.export_bundle(patient, ambulance)
                st.download_button("⬇️ Download FHIR JSON", data=bundle_json,
                                   file_name=f"fhir_referral_{patient.patient_id}.json",
                                   mime="application/json", key=f"fhir_dl_{patient.patient_id}")
                with st.expander("Preview FHIR Bundle"):
                    st.code(bundle_json, language="json")
        with col2:
            if st.button("📤 Send to KenyaEMR", key=f"kemr_{patient.patient_id}", use_container_width=True):
                st.info("KenyaEMR endpoint not configured. Set KENYA_EMR_ENDPOINT in .env to enable.")
        with col3:
            if st.button("📊 Export to DHIS2", key=f"dhis2_{patient.patient_id}", use_container_width=True):
                st.info("DHIS2 endpoint not configured. Set DHIS2_ENDPOINT in .env to enable.")


# =============================================================================
# MOH REFERRAL LETTER SERVICE
# =============================================================================
class MOHReferralLetterService:
    def _generate_qr_code(self, data: str):
        if not QRCODE_AVAILABLE:
            return None
        qr = qrcode.QRCode(version=1, box_size=4, border=2)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf

    def generate_moh_referral_letter(self, patient: Patient, referring_hospital_data: dict,
                                     receiving_hospital_data: dict, ambulance=None):
        if not REPORTLAB_AVAILABLE:
            st.error("ReportLab not available. Cannot generate PDF.")
            return None

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                rightMargin=0.75 * inch, leftMargin=0.75 * inch,
                                topMargin=0.75 * inch, bottomMargin=0.75 * inch)
        story = []
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle('MOHTitle', parent=styles['Heading1'], fontSize=14,
                                     alignment=1, spaceAfter=4, textColor=colors.HexColor('#003087'))
        subtitle_style = ParagraphStyle('MOHSubtitle', parent=styles['Normal'], fontSize=10, alignment=1, spaceAfter=2)
        label_style = ParagraphStyle('Label', parent=styles['Normal'], fontSize=9,
                                     textColor=colors.HexColor('#555555'), fontName='Helvetica-Bold')
        value_style = ParagraphStyle('Value', parent=styles['Normal'], fontSize=10)
        section_style = ParagraphStyle('Section', parent=styles['Heading2'], fontSize=11,
                                       textColor=colors.HexColor('#003087'), spaceBefore=12,
                                       spaceAfter=4, fontName='Helvetica-Bold')
        small_style = ParagraphStyle('Small', parent=styles['Normal'], fontSize=8, textColor=colors.grey)

        story.append(Paragraph("MINISTRY OF HEALTH — REPUBLIC OF KENYA", title_style))
        story.append(Paragraph("COUNTY HEALTH REFERRAL FORM (MOH 367)", title_style))
        story.append(Paragraph("Kisumu County Health Services", subtitle_style))
        story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#003087'), spaceAfter=8))

        moh_ref = patient.moh_referral_number or f"MOH-KSM-{patient.patient_id}-{datetime.utcnow().strftime('%Y%m%d')}"
        triage_str = {'Red': '🔴 Red', 'Orange': '🟠 Orange'}.get(patient.triage_level, '🟢 Green')

        ref_data = [
            [Paragraph("<b>MOH Referral No.:</b>", label_style), Paragraph(moh_ref, value_style),
             Paragraph("<b>Date / Tarehe:</b>", label_style),
             Paragraph(datetime.utcnow().strftime('%d %B %Y %H:%M'), value_style)],
            [Paragraph("<b>Triage Level:</b>", label_style), Paragraph(triage_str, value_style),
             Paragraph("<b>MEWS Score:</b>", label_style), Paragraph(str(patient.mews_score or 0), value_style)],
        ]
        ref_table = Table(ref_data, colWidths=[1.5 * inch, 2 * inch, 1.5 * inch, 2 * inch])
        ref_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#F0F4FF')),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCCCCC')),
            ('PADDING', (0, 0), (-1, -1), 6),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        story.append(ref_table)
        story.append(Spacer(1, 10))

        story.append(Paragraph("1. PATIENT DETAILS / MAELEZO YA MGONJWA", section_style))
        patient_rows = [
            [Paragraph("<b>Full Name / Jina:</b>", label_style), Paragraph(patient.name, value_style),
             Paragraph("<b>Age / Umri:</b>", label_style), Paragraph(str(patient.age), value_style)],
            [Paragraph("<b>National ID / SHA No.:</b>", label_style),
             Paragraph(f"{patient.national_id or 'N/A'} / {patient.sha_member_number or 'N/A'}", value_style),
             Paragraph("<b>SHA Verified:</b>", label_style),
             Paragraph("YES ✅" if patient.sha_verified else "NO — Self Pay / County Subsidy", value_style)],
            [Paragraph("<b>Diagnosis / Utambuzi:</b>", label_style), Paragraph(patient.condition, value_style),
             Paragraph("<b>Allergies / Mzio:</b>", label_style), Paragraph(patient.allergies or 'NKDA', value_style)],
        ]
        pt = Table(patient_rows, colWidths=[1.5 * inch, 2 * inch, 1.5 * inch, 2 * inch])
        pt.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCCCCC')),
                                ('PADDING', (0, 0), (-1, -1), 6), ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
        story.append(pt)
        story.append(Spacer(1, 8))

        story.append(Paragraph("2. VITAL SIGNS AT REFERRAL / DALILI ZA UHAI", section_style))
        if patient.vital_signs:
            vs = patient.vital_signs
            vitals_data = [[
                Paragraph("<b>BP</b>", label_style), Paragraph(str(vs.get('blood_pressure', 'N/A')), value_style),
                Paragraph("<b>HR (bpm)</b>", label_style), Paragraph(str(vs.get('heart_rate', 'N/A')), value_style),
                Paragraph("<b>Temp (°C)</b>", label_style), Paragraph(str(vs.get('temperature', 'N/A')), value_style),
                Paragraph("<b>SpO₂ (%)</b>", label_style), Paragraph(str(vs.get('oxygen_saturation', 'N/A')), value_style),
            ]]
            vt = Table(vitals_data, colWidths=[0.7*inch, 0.85*inch, 0.85*inch, 0.85*inch,
                                               0.9*inch, 0.85*inch, 0.85*inch, 0.85*inch])
            vt.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCCCCC')),
                                    ('PADDING', (0, 0), (-1, -1), 5),
                                    ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#FFF9F0'))]))
            story.append(vt)
        else:
            story.append(Paragraph("Vital signs not recorded at time of referral.", small_style))
        story.append(Spacer(1, 8))

        story.append(Paragraph("3. CLINICAL SUMMARY / MUHTASARI WA KLINIKI", section_style))
        notes_data = [
            [Paragraph("<b>Medical History:</b>", label_style), Paragraph(patient.medical_history or 'Nil reported', value_style)],
            [Paragraph("<b>Current Medications:</b>", label_style), Paragraph(patient.current_medications or 'Nil', value_style)],
            [Paragraph("<b>Reason for Referral:</b>", label_style),
             Paragraph(patient.notes or f'Patient referred for specialised management of {patient.condition}', value_style)],
        ]
        nt = Table(notes_data, colWidths=[1.5 * inch, 5.5 * inch])
        nt.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCCCCC')),
                                ('PADDING', (0, 0), (-1, -1), 6), ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
        story.append(nt)
        story.append(Spacer(1, 8))

        story.append(Paragraph("4. REFERRING & RECEIVING FACILITY / VITUO VYA AFYA", section_style))
        fac_data = [
            [Paragraph("<b>Referring Facility:</b>", label_style),
             Paragraph(referring_hospital_data.get('facility_name', patient.referring_hospital), value_style),
             Paragraph("<b>Receiving Facility:</b>", label_style),
             Paragraph(receiving_hospital_data.get('facility_name', patient.receiving_hospital), value_style)],
            [Paragraph("<b>Contact:</b>", label_style),
             Paragraph(referring_hospital_data.get('contact_number', 'N/A'), value_style),
             Paragraph("<b>Contact:</b>", label_style),
             Paragraph(receiving_hospital_data.get('contact_number', 'N/A'), value_style)],
            [Paragraph("<b>Referring Physician:</b>", label_style), Paragraph(patient.referring_physician, value_style),
             Paragraph("<b>Receiving Physician:</b>", label_style),
             Paragraph(patient.receiving_physician or 'On call clinician', value_style)],
        ]
        if ambulance:
            fac_data.append([Paragraph("<b>Ambulance ID:</b>", label_style), Paragraph(ambulance.ambulance_id, value_style),
                             Paragraph("<b>Driver:</b>", label_style), Paragraph(ambulance.driver_name, value_style)])
        ft = Table(fac_data, colWidths=[1.5 * inch, 2 * inch, 1.5 * inch, 2 * inch])
        ft.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCCCCC')),
                                ('PADDING', (0, 0), (-1, -1), 6), ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
        story.append(ft)
        story.append(Spacer(1, 8))

        story.append(Paragraph("5. SHA / SHIF BILLING INFORMATION", section_style))
        sha_status = "VERIFIED ✅" if patient.sha_verified else "UNVERIFIED — SELF PAY"
        sha_data = [
            [Paragraph("<b>SHA Member No.:</b>", label_style), Paragraph(patient.sha_member_number or 'N/A', value_style),
             Paragraph("<b>SHA Claim ID:</b>", label_style), Paragraph(patient.sha_claim_id or 'Pending', value_style)],
            [Paragraph("<b>SHA Status:</b>", label_style), Paragraph(sha_status, value_style),
             Paragraph("<b>Billing Amount:</b>", label_style),
             Paragraph(f"KSh {patient.sha_billing_amount_kes:,.2f}" if patient.sha_billing_amount_kes else 'Calculated on completion', value_style)],
        ]
        st_table = Table(sha_data, colWidths=[1.5 * inch, 2 * inch, 1.5 * inch, 2 * inch])
        st_table.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCCCCC')),
                                      ('PADDING', (0, 0), (-1, -1), 6),
                                      ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#E1F5EE'))]))
        story.append(st_table)
        story.append(Spacer(1, 10))

        story.append(Paragraph("6. AUTHORISATION / IDHINI", section_style))
        sig_data = [[
            Paragraph("<b>Referring Physician Signature:</b><br/><br/>___________________________<br/>Name: "
                      + patient.referring_physician + "<br/>Date: " + datetime.utcnow().strftime('%d/%m/%Y'), label_style),
            Paragraph("<b>Stamp / Muhuri wa Kituo:</b><br/><br/><br/>", label_style),
            Paragraph("<b>Receiving Physician Signature:</b><br/><br/>___________________________<br/>"
                      "Name: (On Arrival)<br/>Date: ___________", label_style),
        ]]
        sig_table = Table(sig_data, colWidths=[2.4 * inch, 2.4 * inch, 2.4 * inch])
        sig_table.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCCCCC')),
                                       ('PADDING', (0, 0), (-1, -1), 10), ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
        story.append(sig_table)
        story.append(Spacer(1, 10))

        qr_url = f"https://kisumu.go.ke/referral/verify/{patient.patient_id}"
        qr_buf = self._generate_qr_code(qr_url)
        if qr_buf and REPORTLAB_AVAILABLE:
            from reportlab.platypus import Image as RLImage
            qr_img = RLImage(qr_buf, width=1.2 * inch, height=1.2 * inch)
            qr_section = Table([[qr_img, Paragraph(
                f"<b>Digital Verification QR Code</b><br/>Scan to verify this referral online:<br/>{qr_url}<br/><br/>"
                f"Patient ID: {patient.patient_id}<br/>"
                f"Generated: {datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC", small_style
            )]], colWidths=[1.4 * inch, 5.8 * inch])
            qr_section.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                                            ('PADDING', (0, 0), (-1, -1), 6)]))
            story.append(qr_section)

        story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#CCCCCC'), spaceAfter=4))
        story.append(Paragraph(
            "This document is generated by the Kisumu County Hospital Referral System. "
            "It is a legal referral document under the Kenya Health Act 2017. "
            "Retain one copy at the referring facility and present original to the receiving facility.",
            small_style,
        ))
        doc.build(story)
        return buf.getvalue()


# =============================================================================
# OFFLINE SERVICE
# =============================================================================
class OfflineService:
    @staticmethod
    def inject_pwa_manifest():
        st.components.v1.html("""
        <script>
        function updateOnlineStatus() {
            var b = document.getElementById('offline-banner');
            if (b) { b.style.display = navigator.onLine ? 'none' : 'flex'; }
        }
        window.addEventListener('online', updateOnlineStatus);
        window.addEventListener('offline', updateOnlineStatus);
        window.addEventListener('load', updateOnlineStatus);
        </script>
        <div id="offline-banner" style="display:none;background:#854F0B;color:#FAEEDA;
            padding:10px 16px;border-radius:8px;margin-bottom:12px;align-items:center;
            gap:10px;font-size:14px;font-weight:500;">
            ⚠️ <strong>You are offline.</strong>
            Referrals will be queued locally and synced when connection is restored.
        </div>""", height=60)

    @staticmethod
    def render_offline_queue_status(database: Database):
        pending = database.get_pending_offline_actions()
        if pending:
            st.warning(f"📤 {len(pending)} action(s) pending sync from offline mode.")
            with st.expander("View offline queue"):
                for item in pending:
                    st.write(f"- [{item.action_type}] queued at {item.created_at.strftime('%H:%M:%S')}")
            if st.button("🔄 Sync Now", key="sync_offline"):
                synced = 0
                for item in pending:
                    try:
                        if item.action_type == 'CREATE_REFERRAL':
                            database.add_patient(item.payload)
                        database.mark_offline_synced(item.id)
                        synced += 1
                    except Exception as e:
                        database.mark_offline_synced(item.id, error=str(e))
                st.success(f"✅ Synced {synced}/{len(pending)} queued actions.")
                st.rerun()


# =============================================================================
# TRIAGE SERVICE
# =============================================================================
class TriageService:
    TRIAGE_COLORS = {'Red': '🔴 Red — Immediate', 'Orange': '🟠 Orange — Urgent', 'Green': '🟢 Green — Routine'}

    @staticmethod
    def calculate_mews(respiratory_rate, heart_rate, systolic_bp, temperature, consciousness_avpu) -> int:
        score = 0
        if respiratory_rate <= 8 or respiratory_rate >= 30:
            score += 3
        elif respiratory_rate >= 25:
            score += 2
        elif respiratory_rate <= 11 or respiratory_rate >= 21:
            score += 1
        if heart_rate <= 39 or heart_rate >= 130:
            score += 3
        elif heart_rate >= 111 or heart_rate <= 49:
            score += 2
        elif heart_rate >= 101 or heart_rate <= 59:
            score += 1
        if systolic_bp <= 69:
            score += 3
        elif systolic_bp <= 79:
            score += 2
        elif systolic_bp <= 99:
            score += 1
        if temperature <= 35.0 or temperature >= 39.1:
            score += 2
        elif temperature <= 35.9 or temperature >= 38.1:
            score += 1
        score += {'Alert': 0, 'Voice': 1, 'Pain': 2, 'Unresponsive': 3}.get(consciousness_avpu, 0)
        return score

    @staticmethod
    def score_to_triage(mews_score: int) -> str:
        if mews_score >= 5:
            return 'Red'
        elif mews_score >= 3:
            return 'Orange'
        return 'Green'

    @staticmethod
    def render_triage_form():
        st.subheader("🏥 Patient Triage — MEWS Score")
        col1, col2 = st.columns(2)
        with col1:
            rr = st.number_input("Respiratory Rate (breaths/min)", min_value=0, max_value=60, value=16, key="mews_rr")
            hr = st.number_input("Heart Rate (bpm)", min_value=0, max_value=250, value=80, key="mews_hr")
            sbp = st.number_input("Systolic BP (mmHg)", min_value=0, max_value=300, value=120, key="mews_sbp")
        with col2:
            temp = st.number_input("Temperature (°C)", min_value=30.0, max_value=45.0, value=36.6, key="mews_temp")
            avpu = st.selectbox("Consciousness (AVPU)", ["Alert", "Voice", "Pain", "Unresponsive"], key="mews_avpu")
        score = TriageService.calculate_mews(rr, hr, sbp, temp, avpu)
        triage = TriageService.score_to_triage(score)
        bg = {'Red': '#FCEBEB', 'Orange': '#FAEEDA', 'Green': '#EAF3DE'}[triage]
        tc = {'Red': '#A32D2D', 'Orange': '#854F0B', 'Green': '#3B6D11'}[triage]
        msg = {'Red': '🚨 Immediate escalation required. ALS ambulance mandatory.',
               'Orange': '⚡ Urgent transfer within 30 minutes.',
               'Green': '✅ Routine transfer. Standard ambulance appropriate.'}[triage]
        st.markdown(
            f'<div style="background:{bg};padding:12px 16px;border-radius:8px;margin-top:8px;">'
            f'<b style="color:{tc};font-size:16px;">MEWS Score: {score} — {TriageService.TRIAGE_COLORS[triage]}</b><br>'
            f'<span style="font-size:12px;color:{tc};">{msg}</span></div>', unsafe_allow_html=True,
        )
        return score, triage, {'respiratory_rate': rr, 'heart_rate': hr, 'systolic_bp': sbp,
                                'temperature': temp, 'consciousness': avpu}


# =============================================================================
# ANALYTICS SERVICE
# =============================================================================
class AnalyticsService:
    def __init__(self, database: Database):
        self.database = database

    def get_kpis(self) -> dict:
        patients = self.database.get_all_patients()
        ambulances = self.database.get_all_ambulances()
        total = len(patients)
        active = len([p for p in patients if p.status not in ['Arrived at Destination', 'Completed']])
        avail_amb = len([a for a in ambulances if a.status == 'Available'])
        return {
            'total_referrals': total,
            'active_referrals': active,
            'available_ambulances': avail_amb,
            'avg_response_time': "15.0 min",
            'completion_rate': (f"{(total - active) / total * 100:.1f}%" if total > 0 else "0%"),
        }

    def get_referral_trends(self) -> pd.DataFrame:
        patients = self.database.get_all_patients()
        if not patients:
            return pd.DataFrame()
        df = pd.DataFrame([{'date': p.referral_time.date()} for p in patients])
        return df.groupby('date').size().reset_index(name='count')

    def get_hospital_stats(self) -> pd.DataFrame:
        patients = self.database.get_all_patients()
        if not patients:
            return pd.DataFrame()
        df = pd.DataFrame([{'hospital': p.referring_hospital, 'status': p.status} for p in patients])
        return df.groupby(['hospital', 'status']).size().reset_index(name='count')


# =============================================================================
# NOTIFICATION SERVICE
# =============================================================================
class NotificationService:
    def __init__(self, database: Database):
        self.database = database

    def send_notification(self, recipient, message, notification_type):
        subjects = {
            'referral': 'New Patient Referral', 'dispatch': 'Ambulance Dispatched',
            'arrival': 'Patient Arrival Notification', 'pickup': 'Patient Picked Up - Ambulance En Route',
            'emergency': '🚨 EMERGENCY ALERT',
        }
        st.success(f"📧 Notification prepared: {subjects.get(notification_type, 'Notification')} — {message}")
        return True

    def send_pickup_notification_to_driver(self, patient: Patient, ambulance: Ambulance):
        message = (f"New patient pickup: {patient.name} at {patient.referring_hospital}. "
                   f"Condition: {patient.condition}. Please proceed to pick up the patient.")
        self.database.add_communication({'patient_id': patient.patient_id,
                                         'ambulance_id': ambulance.ambulance_id,
                                         'sender': 'System', 'receiver': ambulance.driver_name,
                                         'message': message, 'message_type': 'pickup_notification'})
        st.success(f"🚑 Pickup notification sent to driver {ambulance.driver_name}")
        return True

    def send_enroute_notification_to_hospital(self, patient: Patient, ambulance: Ambulance):
        message = (f"Ambulance {ambulance.ambulance_id} is en route with patient {patient.name}. "
                   f"Condition: {patient.condition}. ETA: 15-20 minutes.")
        self.database.add_communication({'patient_id': patient.patient_id,
                                         'ambulance_id': ambulance.ambulance_id,
                                         'sender': 'System', 'receiver': patient.receiving_hospital,
                                         'message': message, 'message_type': 'enroute_notification'})
        st.success(f"🏥 Enroute notification sent to {patient.receiving_hospital}")
        return True


# =============================================================================
# REFERRAL SERVICE
# =============================================================================
class ReferralService:
    def __init__(self, database: Database, notification_service: NotificationService):
        self.database = database
        self.notification_service = notification_service

    def create_referral(self, patient_data: dict, user: dict):
        try:
            patient_data['created_by'] = user['role']
            patient = self.database.add_patient(patient_data)
            self.database.add_referral({'patient_id': patient.patient_id,
                                        'ambulance_id': patient_data.get('assigned_ambulance'),
                                        'created_by': user['role']})
            self.database.log_action(user['role'], user['role'], 'CREATE_REFERRAL', 'Patient',
                                     patient.patient_id, f"Condition: {patient_data.get('condition')}")
            return patient
        except Exception as e:
            st.error(f"Error creating referral: {e}")
            return None

    def assign_ambulance(self, patient_id: str, ambulance_id: str) -> bool:
        try:
            Patient.objects.filter(patient_id=patient_id).update(
                assigned_ambulance=ambulance_id, status='Ambulance Assigned'
            )
            Ambulance.objects.filter(ambulance_id=ambulance_id).update(
                status='On Transfer', current_patient=patient_id
            )
            return True
        except Exception as e:
            st.error(f"Error assigning ambulance: {e}")
            return False

    def auto_assign_nearest_ambulance(self, patient_id: str, require_als=False) -> bool:
        patient = self.database.get_patient_by_id(patient_id)
        if not patient or not patient.referring_hospital_lat or not patient.referring_hospital_lng:
            st.error("Patient or hospital location data missing")
            return False
        nearest = self.database.find_nearest_ambulance(patient.referring_hospital_lat,
                                                        patient.referring_hospital_lng,
                                                        require_als=require_als)
        if not nearest:
            st.error("No available ambulances with sufficient fuel")
            return False
        Patient.objects.filter(patient_id=patient_id).update(
            assigned_ambulance=nearest.ambulance_id, status='Ambulance Assigned'
        )
        Ambulance.objects.filter(ambulance_id=nearest.ambulance_id).update(
            status='On Transfer', current_patient=patient_id,
            destination=patient.receiving_hospital
        )
        # Refresh for notification
        nearest.refresh_from_db()
        patient.refresh_from_db()
        self.notification_service.send_pickup_notification_to_driver(patient, nearest)
        st.success(f"🚑 Nearest ambulance {nearest.ambulance_id} assigned to patient {patient.name}")
        return True

    def mark_patient_picked_up(self, patient_id: str) -> bool:
        patient = self.database.get_patient_by_id(patient_id)
        if not patient:
            st.error("Patient not found")
            return False
        ambulance = Ambulance.objects.filter(ambulance_id=patient.assigned_ambulance).first()
        if not ambulance:
            st.error("Assigned ambulance not found")
            return False
        Patient.objects.filter(patient_id=patient_id).update(
            status='Patient Picked Up', pickup_notification_sent=True
        )
        self.notification_service.send_enroute_notification_to_hospital(patient, ambulance)
        st.success(f"✅ Patient {patient.name} marked as picked up. Receiving hospital notified.")
        return True


# =============================================================================
# AMBULANCE SERVICE
# =============================================================================
class AmbulanceService:
    def __init__(self, database: Database):
        self.database = database

    def get_available_ambulances_df(self) -> pd.DataFrame:
        ambulances = self.database.get_available_ambulances()
        data = [{'Ambulance ID': a.ambulance_id, 'Driver': a.driver_name, 'Contact': a.driver_contact,
                 'Location': a.current_location, 'Status': a.status, 'Fuel Level': f"{a.fuel_level:.1f}%"}
                for a in ambulances]
        return pd.DataFrame(data)

    def update_ambulance_location(self, ambulance_id, latitude, longitude, location_name, patient_id=None) -> bool:
        try:
            Ambulance.objects.filter(ambulance_id=ambulance_id).update(
                latitude=latitude, longitude=longitude,
                current_location=location_name, last_location_update=datetime.utcnow()
            )
            self.database.add_location_update({'ambulance_id': ambulance_id, 'latitude': latitude,
                                               'longitude': longitude, 'location_name': location_name,
                                               'patient_id': patient_id})
            return True
        except Exception as e:
            st.error(f"Error updating ambulance location: {e}")
            return False

    def get_ambulance_with_fuel_info(self, ambulance_id: str):
        amb = Ambulance.objects.filter(ambulance_id=ambulance_id).first()
        if not amb:
            return None
        if amb.fuel_level > 50:
            fuel_status = "🟢 Good"
        elif amb.fuel_level > 20:
            fuel_status = "🟡 Low"
        else:
            fuel_status = "🔴 Critical"
        return {'ambulance': amb, 'fuel_level': amb.fuel_level, 'fuel_status': fuel_status}


# =============================================================================
# LOCATION SIMULATOR
# =============================================================================
class LocationSimulator:
    def __init__(self, database: Database):
        self.database = database
        self.running = False

    def start_simulation(self, ambulance_id, patient_id, start_lat, start_lng, end_lat, end_lng):
        self.running = True
        amb_svc = AmbulanceService(self.database)
        total_dist = self.database.calculate_distance(start_lat, start_lng, end_lat, end_lng)
        steps = 20
        lat_step = (end_lat - start_lat) / steps
        lng_step = (end_lng - start_lng) / steps
        for step in range(steps + 1):
            if not self.running:
                break
            amb_svc.update_ambulance_location(
                ambulance_id, start_lat + lat_step * step, start_lng + lng_step * step,
                f"En route - Step {step}/{steps}", patient_id,
            )
            if step > 0:
                self.database.update_ambulance_fuel(ambulance_id, total_dist / steps)
            time.sleep(5)
        if self.running:
            Ambulance.objects.filter(ambulance_id=ambulance_id).update(status='Available', current_patient=None)

    def stop_simulation(self):
        self.running = False


# =============================================================================
# MAP UTILS
# =============================================================================
class MapUtils:
    @staticmethod
    def create_uber_style_map(patient: Patient, ambulance: Ambulance, hosp_df: pd.DataFrame):
        if not PYDECK_AVAILABLE or not ambulance or not patient:
            return None
        try:
            ref_row = hosp_df[hosp_df['facility_name'] == patient.referring_hospital].iloc[0]
            rec_row = hosp_df[hosp_df['facility_name'] == patient.receiving_hospital].iloc[0]
        except IndexError:
            return None
        hospitals_layer = pdk.Layer('ScatterplotLayer', data=[
            {'name': patient.referring_hospital,
             'coordinates': [ref_row['longitude'], ref_row['latitude']], 'color': [0, 128, 0, 200], 'radius': 300},
            {'name': patient.receiving_hospital,
             'coordinates': [rec_row['longitude'], rec_row['latitude']], 'color': [255, 0, 0, 200], 'radius': 300},
        ], get_position='coordinates', get_color='color', get_radius='radius', pickable=True)
        ambulance_layer = pdk.Layer('ScatterplotLayer', data=[{
            'name': f"Ambulance {ambulance.ambulance_id} - Fuel: {ambulance.fuel_level:.1f}%",
            'coordinates': [ambulance.longitude, ambulance.latitude], 'color': [0, 0, 255, 200], 'radius': 200,
        }], get_position='coordinates', get_color='color', get_radius='radius', pickable=True)
        route_layer = pdk.Layer('LineLayer', data=[{'path': [
            [ref_row['longitude'], ref_row['latitude']],
            [ambulance.longitude, ambulance.latitude],
            [rec_row['longitude'], rec_row['latitude']],
        ], 'color': [255, 165, 0, 150]}], get_path='path', get_color='color', get_width=5, pickable=True)
        center_lat = (ref_row['latitude'] + rec_row['latitude'] + ambulance.latitude) / 3
        center_lng = (ref_row['longitude'] + rec_row['longitude'] + ambulance.longitude) / 3
        view_state = pdk.ViewState(latitude=center_lat, longitude=center_lng, zoom=11, pitch=0)
        return pdk.Deck(layers=[hospitals_layer, ambulance_layer, route_layer],
                        initial_view_state=view_state,
                        tooltip={'html': '<b>{name}</b>', 'style': {'color': 'white'}})

    @staticmethod
    def create_real_time_tracking_map(patient: Patient, ambulance, hosp_df: pd.DataFrame):
        if not ambulance or not patient:
            st.info("Waiting for ambulance assignment...")
            return
        try:
            ref_row = hosp_df[hosp_df['facility_name'] == patient.referring_hospital].iloc[0]
            rec_row = hosp_df[hosp_df['facility_name'] == patient.receiving_hospital].iloc[0]
        except IndexError:
            st.error("Hospital data not found.")
            return
        col1, col2, col3, col4 = st.columns(4)
        with col1: st.metric("Ambulance", ambulance.ambulance_id)
        with col2: st.metric("Driver", ambulance.driver_name)
        with col3:
            fuel_status = "🟢 Good" if ambulance.fuel_level > 50 else "🟡 Low" if ambulance.fuel_level > 20 else "🔴 Critical"
            st.metric("Fuel Level", f"{ambulance.fuel_level:.1f}%", fuel_status)
        with col4: st.metric("Status", ambulance.status)

        if Config.GOOGLE_MAPS_API_KEY and ambulance.latitude and ambulance.longitude:
            st.subheader("📍 Live Ambulance Tracking on Google Maps")
            map_html = (f'<iframe width="100%" height="500" frameborder="0" style="border:0" '
                        f'src="https://www.google.com/maps/embed/v1/view?key={Config.GOOGLE_MAPS_API_KEY}'
                        f'&center={ambulance.latitude},{ambulance.longitude}&zoom=13&maptype=roadmap" '
                        f'allowfullscreen></iframe>')
            st.components.v1.html(map_html, height=520)
        else:
            st.subheader("📍 Live Ambulance Tracking")
            if PYDECK_AVAILABLE:
                map_obj = MapUtils.create_uber_style_map(patient, ambulance, hosp_df)
                if map_obj:
                    st.pydeck_chart(map_obj)
            elif ambulance.latitude and ambulance.longitude:
                map_data = pd.DataFrame({'lat': [ambulance.latitude, ref_row['latitude'], rec_row['latitude']],
                                         'lon': [ambulance.longitude, ref_row['longitude'], rec_row['longitude']]})
                st.map(map_data)


# =============================================================================
# HOSPITALS DATA
# =============================================================================
hospitals_data = {
    'facility_name': [
        'Jaramogi Oginga Odinga Teaching & Referral Hospital (JOOTRH)',
        'Kisumu County Referral Hospital', 'Lumumba Sub-County Hospital', 'Ahero Sub-County Hospital',
        'Kombewa Sub-County / District Hospital', 'Muhoroni County Hospital', 'Nyakach Sub-County Hospital',
        'Chulaimbo Sub-County Hospital', 'Masogo Sub-County (Sub-District) Hospital', 'Nyando District Hospital',
        'Ober Kamoth Sub-County Hospital', 'Rabuor Sub-County Hospital', 'Nyangoma Sub-County Hospital',
        'Nyahera Sub-County Hospital', 'Katito Sub-County Hospital', 'Gita Sub-County Hospital',
        'Masogo Health Centre', 'Victoria Hospital (public) Kisumu', 'Kodiaga Prison Health Centre',
        'Kisumu District Hospital', 'Migosi Health Centre', 'Katito Health Centre', 'Mbaka Oromo Health Centre',
        'Migere Health Centre', 'Milenye Health Centre', 'Minyange Dispensary', 'Nduru Kadero Health Centre',
        'Newa Dispensary', 'Nyakoko Dispensary', 'Ojola Sub-County Hospital', 'Simba Opepo Health Centre',
        'Songhor Health Centre', 'St Marks Lela Health Centre', 'Maseno University Health Centre',
        'Geta Health Centre', 'Kadinda Health Centre', 'Kochieng Health Centre', 'Kodingo Health Centre',
        'Kolenyo Health Centre', 'Kandu Health Centre',
    ],
    'latitude': [
        -0.0754, -0.0754, -0.1058, -0.1743, -0.1813, -0.1551, -0.2670, -0.1848, -0.1855, -0.3573,
        -0.3789, -0.2138, -0.1625, -0.1565, -0.4533, -0.3735, -0.1855, -0.0878, -0.0607, -0.0916,
        -0.1073, -0.4533, -0.2628, -0.1225, -0.1872, -0.2192, -0.1356, -0.2014, -0.2678, -0.1578,
        -0.3381, -0.2131, -0.0803, -0.0025, -0.4739, -0.2167, -0.3658, -0.0956, -0.4536, -0.2314,
    ],
    'longitude': [
        34.7695, 34.7695, 34.7568, 34.9169, 34.6326, 35.1985, 35.0569, 34.6163, 35.0386, 35.0006,
        35.0299, 34.8817, 34.7794, 34.7508, 34.9561, 34.9676, 35.0386, 34.7686, 34.7509, 34.7647,
        34.7794, 34.9561, 34.6061, 34.7553, 34.7781, 34.8331, 34.7381, 34.8289, 34.9981, 34.8419,
        34.9456, 35.1611, 34.6569, 34.6053, 34.9519, 34.8419, 34.9606, 34.7658, 34.9564, 34.8489,
    ],
    'facility_type': [
        'Referral Hospital', 'Referral Hospital', 'Sub-County Hospital', 'Sub-County Hospital',
        'Sub-County Hospital', 'County Hospital', 'Sub-County Hospital', 'Sub-County Hospital',
        'Sub-County Hospital', 'District Hospital', 'Sub-County Hospital', 'Sub-County Hospital',
        'Sub-County Hospital', 'Sub-County Hospital', 'Sub-County Hospital', 'Sub-County Hospital',
        'Health Centre', 'Private Hospital', 'Prison Health Centre', 'District Hospital', 'Health Centre',
        'Health Centre', 'Health Centre', 'Health Centre', 'Health Centre', 'Dispensary', 'Health Centre',
        'Dispensary', 'Dispensary', 'Sub-County Hospital', 'Health Centre', 'Health Centre', 'Health Centre',
        'University Health Centre', 'Health Centre', 'Health Centre', 'Health Centre', 'Health Centre',
        'Health Centre', 'Health Centre',
    ],
    'capacity': [
        500, 400, 100, 100, 100, 75, 75, 78, 77, 80, 70, 60, 65, 50, 52, 40, 42, 30, 35, 20, 20, 25,
        15, 24, 15, 10, 19, 5, 19, 10, 5, 15, 17, 16, 45, 30, 29, 55, 30, 30,
    ],
    'ambulance_services': ['Available', 'Available'] + ['Limited'] * 38,
    'contact_number': [
        '+254-57-2055000', '+254-57-2021578', '+254-57-2023456', '+254-57-2034567', '+254-57-2045678',
        '+254-57-2056789', '+254-57-2067890', '+254-57-2078901', '+254-57-2089012', '+254-57-2090123',
        '+254-57-2101234', '+254-57-2112345', '+254-57-2123456', '+254-57-2134567', '+254-57-2145678',
        '+254-57-2156789', '+254-57-2167890', '+254-57-2178901', '+254-57-2189012', '+254-57-2190123',
        '+254-57-2201234', '+254-57-2212345', '+254-57-2223456', '+254-57-2234567', '+254-57-2245678',
        '+254-57-2256789', '+254-57-2267890', '+254-57-2278901', '+254-57-2289012', '+254-57-2290123',
        '+254-57-2301234', '+254-57-2312345', '+254-57-2323456', '+254-57-2334567', '+254-57-2345678',
        '+254-57-2356789', '+254-57-2367890', '+254-57-2378901', '+254-57-2389012', '+254-57-2390123',
    ],
}
hospitals_df = pd.DataFrame(hospitals_data)

ambulances_data = {
    'ambulance_id': ['KBA 453D', 'KBC 217F', 'KBD 389G', 'KBE 142H', 'KBF 561J', 'KBG 774K',
                     'KBH 238L', 'KBJ 965M', 'KBK 482N', 'KBL 751P', 'KBM 312Q', 'KBN 864R',
                     'KBP 459S', 'KBQ 287T', 'KBR 913U', 'KBS 506V', 'KBT 678W', 'KBU 134X',
                     'KBV 925Y', 'KBX 743Z'],
    'current_location': (
        ['Jaramogi Oginga Odinga Teaching & Referral Hospital (JOOTRH)'] * 10 +
        ['Kisumu County Referral Hospital'] * 7 +
        ['Lumumba Sub-County Hospital'] * 2 +
        ['Ahero Sub-County Hospital'] * 1
    ),
    'latitude': [-0.0754] * 10 + [-0.0754] * 7 + [-0.1058] * 2 + [-0.1743] * 1,
    'longitude': [34.7695] * 10 + [34.7695] * 7 + [34.7568] * 2 + [34.9169] * 1,
    'driver_name': [
        'John Omondi', 'Mary Achieng', 'Paul Otieno', 'Susan Akinyi', 'David Owino',
        'James Okoth', 'Grace Atieno', 'Peter Onyango', 'Alice Adhiambo', 'Robert Ochieng',
        'Sarah Nyongesa', 'Michael Odhiambo', 'Elizabeth Awuor', 'Daniel Omondi', 'Lucy Anyango',
        'Brian Ouma', 'Patricia Adongo', 'Samuel Owuor', 'Rebecca Aoko', 'Kevin Onyango',
    ],
    'driver_contact': [
        '+254712345678', '+254723456789', '+254734567890', '+254745678901', '+254756789012',
        '+254767890123', '+254778901234', '+254789012345', '+254790123456', '+254701234567',
        '+254712345679', '+254723456780', '+254734567891', '+254745678902', '+254756789013',
        '+254767890124', '+254778901235', '+254789012346', '+254790123457', '+254701234568',
    ],
    'fuel_level': [
        85.5, 92.3, 78.9, 65.2, 88.7, 94.1, 71.8, 83.4, 79.6, 86.9,
        90.2, 67.8, 82.5, 75.9, 88.3, 69.7, 91.4, 84.2, 77.5, 80.8,
    ],
}


def initialize_sample_data():
    """Seed ambulances, bed capacity, and SHA members if tables are empty."""
    if not Ambulance.objects.exists():
        for idx, amb_id in enumerate(ambulances_data['ambulance_id']):
            Ambulance(
                ambulance_id=amb_id,
                current_location=ambulances_data['current_location'][idx],
                latitude=ambulances_data['latitude'][idx],
                longitude=ambulances_data['longitude'][idx],
                status='Available',
                driver_name=ambulances_data['driver_name'][idx],
                driver_contact=ambulances_data['driver_contact'][idx],
                fuel_level=ambulances_data['fuel_level'][idx],
            ).save()

    if not BedCapacity.objects.exists():
        BedCapacity(hospital_name='Jaramogi Oginga Odinga Teaching & Referral Hospital (JOOTRH)',
                    total_beds=500, occupied_beds=420, icu_total=40, icu_occupied=35,
                    maternity_total=60, maternity_occupied=50, paediatric_total=50, paediatric_occupied=38,
                    cardiologist_available=True, surgeon_available=True,
                    obstetrician_available=True, paediatrician_available=True).save()
        BedCapacity(hospital_name='Kisumu County Referral Hospital',
                    total_beds=400, occupied_beds=280, icu_total=20, icu_occupied=12,
                    maternity_total=40, maternity_occupied=25, paediatric_total=30, paediatric_occupied=18,
                    cardiologist_available=False, surgeon_available=True,
                    obstetrician_available=True, paediatrician_available=True).save()

    if not SHAMember.objects.exists():
        SHAMember(sha_member_number='SHA-001234', national_id='12345678',
                  member_name='Demo Patient One', active=True, cover_type='SHIF').save()
        SHAMember(sha_member_number='SHA-005678', national_id='87654321',
                  member_name='Demo Patient Two', active=True, cover_type='SHIF').save()
        SHAMember(sha_member_number='SHA-009999', national_id='11223344',
                  member_name='Demo Patient Three', active=False, cover_type='NHIF Legacy').save()


# =============================================================================
# DASHBOARD UI
# =============================================================================
class DashboardUI:
    def __init__(self, database: Database, analytics: AnalyticsService):
        self.database = database
        self.analytics = analytics

    def display(self):
        st.title("📊 Dashboard Overview")
        OfflineService.inject_pwa_manifest()
        OfflineService.render_offline_queue_status(self.database)

        kpis = self.analytics.get_kpis()
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1: st.metric("Total Referrals", kpis['total_referrals'])
        with col2: st.metric("Active Referrals", kpis['active_referrals'])
        with col3: st.metric("Available Ambulances", kpis['available_ambulances'])
        with col4: st.metric("Avg Response Time", kpis['avg_response_time'])
        with col5: st.metric("Completion Rate", kpis['completion_rate'])

        st.markdown("---")
        st.subheader("🏥 Referral Hospital Capacity")
        referral_hospitals = ['Jaramogi Oginga Odinga Teaching & Referral Hospital (JOOTRH)',
                              'Kisumu County Referral Hospital']
        cap_cols = st.columns(2)
        for i, hosp in enumerate(referral_hospitals):
            cap = self.database.get_bed_capacity(hosp)
            with cap_cols[i]:
                if cap:
                    occ_pct = (cap.occupied_beds / cap.total_beds * 100) if cap.total_beds > 0 else 0
                    if occ_pct >= 90:
                        status_color, bg = "🔴", '#FCEBEB'
                    elif occ_pct >= 70:
                        status_color, bg = "🟡", '#FAEEDA'
                    else:
                        status_color, bg = "🟢", '#EAF3DE'
                    short_name = hosp.split('(')[0].strip()
                    specialists = ([f'{s} ✅' for s, a in
                                    [('Cardiologist', cap.cardiologist_available), ('Surgeon', cap.surgeon_available),
                                     ('OB/GYN', cap.obstetrician_available)] if a])
                    spec_str = ' '.join(specialists) if specialists else 'None available'
                    st.markdown(
                        f'<div style="background:{bg};padding:12px;border-radius:8px;">'
                        f'<b>{status_color} {short_name}</b><br>'
                        f'Beds: {cap.occupied_beds}/{cap.total_beds} ({occ_pct:.0f}% occupied)<br>'
                        f'ICU: {cap.icu_occupied}/{cap.icu_total} &nbsp;|&nbsp; '
                        f'Maternity: {cap.maternity_occupied}/{cap.maternity_total}<br>'
                        f'Specialists: {spec_str}</div>', unsafe_allow_html=True,
                    )
                else:
                    st.info(f"No capacity data for {hosp}")

        st.markdown("---")
        col1, col2 = st.columns(2)
        with col1: self.display_referral_trends()
        with col2: self.display_ambulance_status()
        st.subheader("Recent Referrals")
        self.display_recent_referrals()

    def display_referral_trends(self):
        st.subheader("Referral Trends")
        if not PLOTLY_AVAILABLE:
            st.info("Plotly not available for charts.")
            return
        trends = self.analytics.get_referral_trends()
        if not trends.empty:
            st.plotly_chart(px.line(trends, x='date', y='count', title="Daily Referral Trends"),
                            use_container_width=True, key="referral_trends_chart")
        else:
            st.info("No referral data available")

    def display_ambulance_status(self):
        st.subheader("Ambulance Status")
        if not PLOTLY_AVAILABLE:
            st.info("Plotly not available for charts.")
            return
        ambulances = self.database.get_all_ambulances()
        if ambulances:
            status_counts = {}
            for a in ambulances:
                status_counts[a.status] = status_counts.get(a.status, 0) + 1
            st.plotly_chart(px.pie(values=list(status_counts.values()), names=list(status_counts.keys()),
                                   title="Ambulance Status Distribution"),
                            use_container_width=True, key="ambulance_status_chart")
        else:
            st.info("No ambulance data available")

    def display_recent_referrals(self):
        patients = sorted(self.database.get_all_patients(), key=lambda x: x.referral_time, reverse=True)[:5]
        if patients:
            data = []
            for p in patients:
                triage_icon = {'Red': '🔴', 'Orange': '🟠', 'Green': '🟢'}.get(p.triage_level, '⚪')
                data.append({'Patient ID': p.patient_id, 'Name': p.name,
                             'Triage': f"{triage_icon} {p.triage_level}", 'Condition': p.condition,
                             'From': p.referring_hospital, 'To': p.receiving_hospital,
                             'Status': p.status, 'SHA': '✅' if p.sha_verified else '❌',
                             'Time': p.referral_time.strftime('%Y-%m-%d %H:%M')})
            st.dataframe(pd.DataFrame(data), use_container_width=True)
        else:
            st.info("No recent referrals")


# =============================================================================
# REFERRAL UI
# =============================================================================
class ReferralUI:
    def __init__(self, database: Database, notification_service: NotificationService):
        self.database = database
        self.notification_service = notification_service
        self.referral_service = ReferralService(database, notification_service)
        self.sha_service = SHABillingService(database)
        self.moh_service = MOHReferralLetterService()
        self.bed_service = BedCapacityService(database)

    def display(self):
        st.title("📋 Patient Referral Management")
        tab1, tab2, tab3 = st.tabs(["Create Referral", "Active Referrals", "Referral History"])
        with tab1: self.create_referral_form()
        with tab2: self.display_active_referrals()
        with tab3: self.display_referral_history()

    def get_receiving_hospitals(self, user_hospital):
        return ["Jaramogi Oginga Odinga Teaching & Referral Hospital (JOOTRH)",
                "Kisumu County Referral Hospital"]

    def get_referring_hospitals(self, user_hospital):
        if user_hospital in ["All Facilities",
                             "Jaramogi Oginga Odinga Teaching & Referral Hospital (JOOTRH)",
                             "Kisumu County Referral Hospital"]:
            return hospitals_data['facility_name']
        return [user_hospital]

    def create_referral_form(self):
        st.subheader("Create New Patient Referral")
        user_hospital = st.session_state.user['hospital']

        with st.form("referral_form", clear_on_submit=True):
            col1, col2 = st.columns(2)
            with col1:
                name = st.text_input("Patient Name*")
                age = st.number_input("Age*", min_value=0, max_value=120, value=30)
                condition = st.text_input("Medical Condition*")
                referring_physician = st.text_input("Referring Physician*")
                referring_hospital = st.selectbox("Referring Hospital*", self.get_referring_hospitals(user_hospital))
            with col2:
                receiving_hospital = st.selectbox("Receiving Hospital*", self.get_receiving_hospitals(user_hospital))
                receiving_physician = st.text_input("Receiving Physician")
                if referring_hospital == receiving_hospital:
                    st.warning("⚠️ Referring and receiving hospitals cannot be the same.")
                if receiving_hospital:
                    badge, color = self.bed_service.get_capacity_badge(receiving_hospital)
                    bg = {'red': '#FCEBEB', 'amber': '#FAEEDA', 'green': '#EAF3DE', 'unknown': '#F5F5F5'}[color]
                    st.markdown(
                        f'<div style="background:{bg};padding:6px 10px;border-radius:6px;font-size:13px;">'
                        f'<b>Receiving Hospital Capacity:</b> {badge}</div>', unsafe_allow_html=True,
                    )
                    if color == 'red':
                        st.error("⚠️ Receiving hospital is at or above 90% capacity. Consider alternatives.")

            notes = st.text_area("Clinical Notes")
            with st.expander("Additional Medical Information"):
                medical_history = st.text_area("Medical History")
                current_medications = st.text_area("Current Medications")
                allergies = st.text_area("Allergies")

            st.markdown("---")
            st.subheader("🏥 Triage Level")
            triage_level = st.selectbox("Select Triage Level*",
                                        ["Green — Routine", "Orange — Urgent", "Red — Immediate"],
                                        help="Use MEWS Calculator tab to compute score before selecting")
            mews_score = st.number_input("MEWS Score", min_value=0, max_value=20, value=0,
                                         help="Calculate using the MEWS Calculator below, then enter here")
            triage_map = {"Green — Routine": "Green", "Orange — Urgent": "Orange", "Red — Immediate": "Red"}
            selected_triage = triage_map[triage_level]
            if selected_triage == 'Red':
                st.error("🚨 RED TRIAGE: Immediate response required. ALS ambulance will be prioritised.")
            elif selected_triage == 'Orange':
                st.warning("⚡ ORANGE TRIAGE: Urgent — transfer should occur within 30 minutes.")

            st.markdown("---")
            st.subheader("🚑 Ambulance Assignment")
            require_als = selected_triage == 'Red'
            if require_als:
                st.warning("🔴 Red triage: system will prioritise Advanced Life Support (ALS) ambulances.")
            assignment_method = st.radio("Assignment Method", ["Auto-assign nearest ambulance", "Manual selection"])
            ambulance_choice = "Auto-assign"
            if assignment_method == "Manual selection":
                available_ambulances = self.database.get_available_ambulances()
                ambulance_options = ["Select ambulance"] + [
                    f"{a.ambulance_id} - {a.driver_name} (Fuel: {a.fuel_level:.1f}%)" for a in available_ambulances
                ]
                ambulance_choice = st.selectbox("Select Ambulance", ambulance_options)

            submitted = st.form_submit_button("Create Referral", use_container_width=True)
            if submitted:
                if not all([name, age, condition, referring_physician, referring_hospital, receiving_hospital]):
                    st.error("Please fill in all required fields (*)")
                elif referring_hospital == receiving_hospital:
                    st.error("Referring and receiving hospitals cannot be the same.")
                else:
                    cap_status, cap_pct = self.database.get_capacity_status(receiving_hospital)
                    if cap_status == 'red':
                        st.error(f"⚠️ {receiving_hospital} is at {cap_pct:.0f}% capacity. "
                                 "Referral blocked. Please choose another facility.")
                    else:
                        try:
                            ref_row = hospitals_df[hospitals_df['facility_name'] == referring_hospital].iloc[0]
                            rec_row = hospitals_df[hospitals_df['facility_name'] == receiving_hospital].iloc[0]
                        except IndexError:
                            st.error("Hospital data not found.")
                            return

                        sha_verified = st.session_state.get('sha_verified', False)
                        sha_member = st.session_state.get('sha_member')
                        moh_ref_num = (f"MOH-KSM-{secrets.token_hex(3).upper()}-"
                                       f"{datetime.utcnow().strftime('%Y%m%d')}")

                        patient_data = {
                            'name': name, 'age': age, 'condition': condition,
                            'referring_hospital': referring_hospital, 'receiving_hospital': receiving_hospital,
                            'referring_physician': referring_physician, 'receiving_physician': receiving_physician,
                            'notes': notes, 'medical_history': medical_history,
                            'current_medications': current_medications, 'allergies': allergies,
                            'status': 'Referred',
                            'referring_hospital_lat': float(ref_row['latitude']),
                            'referring_hospital_lng': float(ref_row['longitude']),
                            'receiving_hospital_lat': float(rec_row['latitude']),
                            'receiving_hospital_lng': float(rec_row['longitude']),
                            'triage_level': selected_triage, 'mews_score': mews_score,
                            'sha_verified': sha_verified,
                            'sha_member_number': sha_member.sha_member_number if sha_member else None,
                            'national_id': sha_member.national_id if sha_member else None,
                            'moh_referral_number': moh_ref_num,
                        }
                        if assignment_method == "Manual selection" and ambulance_choice != "Select ambulance":
                            patient_data['assigned_ambulance'] = ambulance_choice.split(" - ")[0]

                        patient = self.referral_service.create_referral(patient_data, st.session_state.user)
                        if patient:
                            st.success(f"✅ Referral created! Patient ID: **{patient.patient_id}** "
                                       f"| MOH Ref: **{moh_ref_num}**")
                            if assignment_method == "Auto-assign nearest ambulance":
                                if self.referral_service.auto_assign_nearest_ambulance(patient.patient_id,
                                                                                        require_als=require_als):
                                    st.success("🚑 Nearest ambulance automatically assigned and driver notified!")

                            patient.refresh_from_db()
                            if patient.assigned_ambulance:
                                dist = self.database.calculate_distance(
                                    float(ref_row['latitude']), float(ref_row['longitude']),
                                    float(rec_row['latitude']), float(rec_row['longitude'])
                                )
                                claim = self.database.create_sha_claim(patient.patient_id,
                                                                        patient.assigned_ambulance, dist)
                                st.info(f"🏛️ SHA Claim {claim.claim_id} submitted — "
                                        f"KSh {claim.total_amount:,.2f} for {dist:.1f} km")

                            self.notification_service.send_notification(
                                receiving_hospital,
                                f"New patient referral: {name} — {condition} [{selected_triage} triage]",
                                'referral',
                            )

                            if REPORTLAB_AVAILABLE:
                                st.subheader("📄 MOH Referral Letter")
                                try:
                                    fresh_patient = self.database.get_patient_by_id(patient.patient_id)
                                    pdf_bytes = self.moh_service.generate_moh_referral_letter(
                                        fresh_patient, ref_row.to_dict(), rec_row.to_dict()
                                    )
                                    if pdf_bytes:
                                        st.download_button(
                                            label="⬇️ Download MOH Referral Letter (PDF)",
                                            data=pdf_bytes,
                                            file_name=f"MOH_Referral_{patient.patient_id}_{datetime.utcnow().strftime('%Y%m%d')}.pdf",
                                            mime="application/pdf",
                                            key=f"moh_dl_{patient.patient_id}",
                                        )
                                        Patient.objects.filter(patient_id=patient.patient_id).update(
                                            referral_letter_generated=True
                                        )
                                        st.success("✅ MOH-standard referral letter generated with QR verification code.")
                                except Exception as e:
                                    st.error(f"Error generating referral letter: {e}")

        st.markdown("---")
        st.subheader("Pre-Referral Checks")
        tab_sha, tab_mews = st.tabs(["🏛️ SHA Member Verification", "📊 MEWS Calculator"])
        with tab_sha:
            st.info("Verify the patient's SHA/SHIF membership here before creating the referral above.")
            self.sha_service.render_sha_panel()
        with tab_mews:
            st.info("Calculate the MEWS score here, then enter it in the referral form above.")
            score, triage, vitals = TriageService.render_triage_form()
            st.session_state['mews_vitals'] = vitals
            st.session_state['last_mews_score'] = score
            st.session_state['last_triage'] = triage

    def display_active_referrals(self):
        st.subheader("Active Referrals")
        patients = self.database.get_all_patients()
        user_hospital = st.session_state.user['hospital']
        if user_hospital == "All Facilities":
            active_patients = [p for p in patients if p.status not in ['Arrived at Destination', 'Completed']]
        elif user_hospital in ["Jaramogi Oginga Odinga Teaching & Referral Hospital (JOOTRH)",
                               "Kisumu County Referral Hospital"]:
            active_patients = [p for p in patients if p.receiving_hospital == user_hospital
                               and p.status not in ['Arrived at Destination', 'Completed']]
        else:
            active_patients = [p for p in patients if p.referring_hospital == user_hospital
                               and p.status not in ['Arrived at Destination', 'Completed']]

        if active_patients:
            data = []
            for p in active_patients:
                amb_info = ""
                if p.assigned_ambulance:
                    amb_data = AmbulanceService(self.database).get_ambulance_with_fuel_info(p.assigned_ambulance)
                    if amb_data:
                        amb_info = f"{p.assigned_ambulance} ({amb_data['fuel_status']})"
                triage_icon = {'Red': '🔴', 'Orange': '🟠', 'Green': '🟢'}.get(p.triage_level, '⚪')
                data.append({'Patient ID': p.patient_id, 'Name': p.name,
                             'Triage': f"{triage_icon} {p.triage_level or 'N/A'}", 'MEWS': p.mews_score or 0,
                             'Condition': p.condition, 'From': p.referring_hospital, 'To': p.receiving_hospital,
                             'Status': p.status, 'SHA': '✅ Verified' if p.sha_verified else '❌ Unverified',
                             'Claim': p.sha_claim_id or 'None', 'Ambulance': amb_info or 'Not assigned'})
            st.dataframe(pd.DataFrame(data), use_container_width=True)
            st.subheader("Patient Actions")
            for p in active_patients:
                triage_icon = {'Red': '🔴', 'Orange': '🟠', 'Green': '🟢'}.get(p.triage_level, '⚪')
                with st.expander(f"{triage_icon} Actions for {p.name} ({p.patient_id})"):
                    self.display_patient_actions(p)
        else:
            st.info("No active referrals")

    def display_patient_actions(self, patient: Patient):
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            if st.button("Assign Ambulance", key=f"assign_{patient.patient_id}", use_container_width=True):
                st.session_state[f'assign_ambulance_{patient.patient_id}'] = True
            if st.session_state.get(f'assign_ambulance_{patient.patient_id}'):
                available = self.database.get_available_ambulances()
                if available:
                    opts = [f"{a.ambulance_id} - {a.driver_name} (Fuel: {a.fuel_level:.1f}%)" for a in available]
                    sel = st.selectbox("Select Ambulance", opts, key=f"amb_select_{patient.patient_id}")
                    if st.button("Confirm Assignment", key=f"confirm_{patient.patient_id}", use_container_width=True):
                        if self.referral_service.assign_ambulance(patient.patient_id, sel.split(" - ")[0]):
                            st.success("Ambulance assigned successfully!")
                            st.session_state[f'assign_ambulance_{patient.patient_id}'] = False
                            st.rerun()
                else:
                    st.warning("No available ambulances")

        with col2:
            if st.button("Update Status", key=f"status_{patient.patient_id}", use_container_width=True):
                st.session_state[f'update_status_{patient.patient_id}'] = True
            if st.session_state.get(f'update_status_{patient.patient_id}'):
                new_status = st.selectbox("New Status",
                    ["Referred", "Ambulance Dispatched", "Patient Picked Up",
                     "Transporting to Destination", "Arrived at Destination"],
                    key=f"status_select_{patient.patient_id}")
                if st.button("Update", key=f"update_{patient.patient_id}", use_container_width=True):
                    Patient.objects.filter(patient_id=patient.patient_id).update(status=new_status)
                    self.database.log_action(st.session_state.user['role'], st.session_state.user['role'],
                                             'UPDATE_STATUS', 'Patient', patient.patient_id,
                                             f"New status: {new_status}")
                    st.success("Status updated!")
                    st.session_state[f'update_status_{patient.patient_id}'] = False
                    st.rerun()

        with col3:
            if st.button("View Details", key=f"details_{patient.patient_id}", use_container_width=True):
                st.session_state[f'view_details_{patient.patient_id}'] = True
            if st.session_state.get(f'view_details_{patient.patient_id}'):
                st.write(f"**Medical History:** {patient.medical_history}")
                st.write(f"**Medications:** {patient.current_medications}")
                st.write(f"**Allergies:** {patient.allergies}")
                st.write(f"**SHA Claim:** {patient.sha_claim_id or 'None'} — KSh {patient.sha_billing_amount_kes or 0:,.2f}")
                if st.button("Close", key=f"close_{patient.patient_id}", use_container_width=True):
                    st.session_state[f'view_details_{patient.patient_id}'] = False
                    st.rerun()

        with col4:
            if REPORTLAB_AVAILABLE:
                if st.button("📄 MOH Letter", key=f"moh_{patient.patient_id}", use_container_width=True):
                    try:
                        ref_row = hospitals_df[hospitals_df['facility_name'] == patient.referring_hospital].iloc[0]
                        rec_row = hospitals_df[hospitals_df['facility_name'] == patient.receiving_hospital].iloc[0]
                        ambulance = Ambulance.objects.filter(ambulance_id=patient.assigned_ambulance).first() if patient.assigned_ambulance else None
                        pdf_bytes = MOHReferralLetterService().generate_moh_referral_letter(
                            patient, ref_row.to_dict(), rec_row.to_dict(), ambulance
                        )
                        if pdf_bytes:
                            st.download_button("⬇️ Download PDF", data=pdf_bytes,
                                               file_name=f"MOH_{patient.patient_id}.pdf",
                                               mime="application/pdf", key=f"moh_dl2_{patient.patient_id}")
                    except Exception as e:
                        st.error(f"Error: {e}")

        with col5:
            if (st.session_state.user['role'] == 'Ambulance Driver' and
                    patient.assigned_ambulance and patient.status == 'Ambulance Dispatched'):
                if st.button("Mark Patient Picked Up", key=f"pickup_{patient.patient_id}", use_container_width=True):
                    if self.referral_service.mark_patient_picked_up(patient.patient_id):
                        st.rerun()

    def display_referral_history(self):
        st.subheader("Referral History")
        patients = self.database.get_all_patients()
        user_hospital = st.session_state.user['hospital']
        if user_hospital == "All Facilities":
            filtered = patients
        elif user_hospital in ["Jaramogi Oginga Odinga Teaching & Referral Hospital (JOOTRH)",
                               "Kisumu County Referral Hospital"]:
            filtered = [p for p in patients if p.receiving_hospital == user_hospital]
        else:
            filtered = [p for p in patients if p.referring_hospital == user_hospital]

        if filtered:
            data = []
            for p in filtered:
                triage_icon = {'Red': '🔴', 'Orange': '🟠', 'Green': '🟢'}.get(p.triage_level, '⚪')
                data.append({'Patient ID': p.patient_id, 'Name': p.name,
                             'Triage': f"{triage_icon} {p.triage_level or 'N/A'}", 'Condition': p.condition,
                             'From': p.referring_hospital, 'To': p.receiving_hospital, 'Status': p.status,
                             'SHA': '✅' if p.sha_verified else '❌', 'SHA Claim': p.sha_claim_id or 'None',
                             'Billing (KSh)': f"{p.sha_billing_amount_kes:,.2f}" if p.sha_billing_amount_kes else 'N/A',
                             'MOH Ref': p.moh_referral_number or 'N/A',
                             'Referral Time': p.referral_time.strftime('%Y-%m-%d %H:%M'),
                             'Ambulance': p.assigned_ambulance or 'Not assigned'})
            st.dataframe(pd.DataFrame(data), use_container_width=True)
        else:
            st.info("No referral history")


# =============================================================================
# TRACKING UI
# =============================================================================
class TrackingUI:
    def __init__(self, database: Database):
        self.database = database

    def display(self):
        st.title("🚑 Live Ambulance Tracking")
        col1, col2 = st.columns([3, 1])
        with col2:
            if st.button("🔄 Refresh Map", use_container_width=True):
                st.rerun()

        st.markdown("### 🗺️ Real-time Ambulance Tracking")
        patients = self.database.get_all_patients()
        active_transfers = [p for p in patients if p.status in ['Ambulance Dispatched', 'Patient Picked Up',
                                                                  'Transporting to Destination']]
        if active_transfers:
            for patient in active_transfers:
                triage_icon = {'Red': '🔴', 'Orange': '🟠', 'Green': '🟢'}.get(patient.triage_level, '⚪')
                with st.expander(f"{triage_icon} {patient.name} — {patient.condition}", expanded=True):
                    ambulance = None
                    if patient.assigned_ambulance:
                        ambulance = Ambulance.objects.filter(ambulance_id=patient.assigned_ambulance).first()
                    MapUtils.create_real_time_tracking_map(patient, ambulance, hospitals_df)
                    st.subheader("📢 Notification Status")
                    col_not1, col_not2 = st.columns(2)
                    with col_not1:
                        st.metric("Pickup Notification to Driver",
                                  "✅ Sent" if patient.pickup_notification_sent else "⏳ Pending")
                    with col_not2:
                        st.metric("Enroute Notification to Hospital",
                                  "✅ Sent" if patient.enroute_notification_sent else "⏳ Pending")
                    if ambulance:
                        FHIRService.render_fhir_panel(patient, ambulance)
        else:
            st.info("No active patient transfers to track")

        st.markdown("### 🚑 All Ambulances")
        self.display_ambulance_list()

    def display_ambulance_list(self):
        for ambulance in self.database.get_all_ambulances():
            status_color = "🟢" if ambulance.status == 'Available' else "🔴"
            fuel_indicator = "🟢" if ambulance.fuel_level > 50 else "🟡" if ambulance.fuel_level > 20 else "🔴"
            with st.expander(f"{status_color} {ambulance.ambulance_id} — {ambulance.driver_name} "
                             f"{fuel_indicator} Fuel: {ambulance.fuel_level:.1f}%"):
                col1, col2 = st.columns(2)
                with col1:
                    st.write(f"**Status:** {ambulance.status}")
                    st.write(f"**Location:** {ambulance.current_location}")
                    st.write(f"**Contact:** {ambulance.driver_contact}")
                with col2:
                    st.write(f"**Fuel Level:** {ambulance.fuel_level:.1f}%")
                    if ambulance.current_patient:
                        patient = self.database.get_patient_by_id(ambulance.current_patient)
                        if patient:
                            st.write(f"**Current Patient:** {patient.name}")
                            st.write(f"**Destination:** {patient.receiving_hospital}")


# =============================================================================
# SHA BILLING UI
# =============================================================================
class SHABillingUI:
    def __init__(self, database: Database):
        self.database = database
        self.sha_service = SHABillingService(database)

    def display(self):
        st.title("🏛️ SHA / SHIF Billing & Insurance")
        tab1, tab2, tab3 = st.tabs(["Claims Overview", "Tariff Calculator", "SHA Members (Demo)"])
        with tab1: self.display_claims()
        with tab2: self.display_tariff_calculator()
        with tab3: self.display_sha_members()

    def display_claims(self):
        st.subheader("Submitted SHA Claims")
        claims = self.database.get_sha_claims()
        if claims:
            total_billed = sum(c.total_amount for c in claims)
            approved = [c for c in claims if c.status == 'Approved']
            col1, col2, col3 = st.columns(3)
            col1.metric("Total Claims", len(claims))
            col2.metric("Total Billed (KSh)", f"{total_billed:,.2f}")
            col3.metric("Approved", len(approved))
            data = [{'Claim ID': c.claim_id, 'Patient ID': c.patient_id, 'Ambulance': c.ambulance_id,
                     'Distance (km)': c.distance_km, 'Base (KSh)': c.base_charge,
                     'Additional (KSh)': c.additional_charge, 'Total (KSh)': c.total_amount,
                     'Status': c.status, 'Submitted': c.submitted_at.strftime('%Y-%m-%d %H:%M')}
                    for c in claims]
            st.dataframe(pd.DataFrame(data), use_container_width=True)
        else:
            st.info("No SHA claims submitted yet.")

    def display_tariff_calculator(self):
        st.subheader("SHA Ambulance Tariff Calculator")
        st.info(f"Official tariff: KSh {Config.SHA_BASE_CHARGE_KES:,.0f} for first "
                f"{Config.SHA_BASE_DISTANCE_KM:.0f} km, then KSh {Config.SHA_PER_KM_CHARGE_KES:.0f}/km")
        distance = st.slider("Distance (km)", min_value=1, max_value=200, value=30, step=1)
        billing = self.sha_service.calculate_billing(distance)
        col1, col2, col3 = st.columns(3)
        col1.metric("Base Charge", f"KSh {billing['base_charge']:,.0f}")
        col2.metric("Additional Charge", f"KSh {billing['additional_charge']:,.0f}")
        col3.metric("Total Payable by SHA", f"KSh {billing['total']:,.0f}")
        st.caption(billing['tariff_note'])

    def display_sha_members(self):
        st.subheader("Demo SHA Member Registry")
        st.info("In production, this connects to the SHA national member database API. Demo members for testing:")
        members = list(SHAMember.objects.all())
        if members:
            data = [{'SHA Number': m.sha_member_number, 'National ID': m.national_id,
                     'Name': m.member_name, 'Cover': m.cover_type, 'Active': '✅' if m.active else '❌'}
                    for m in members]
            st.dataframe(pd.DataFrame(data), use_container_width=True)


# =============================================================================
# BED MANAGEMENT UI
# =============================================================================
class BedManagementUI:
    def __init__(self, database: Database):
        self.database = database
        self.bed_service = BedCapacityService(database)

    def display(self):
        st.title("🏥 Hospital Bed & Capacity Management")
        tab1, tab2 = st.tabs(["Live Capacity Dashboard", "Update Capacity"])
        with tab1:
            self.bed_service.render_capacity_dashboard()
            st.markdown("---")
            self.display_capacity_chart()
        with tab2:
            self.bed_service.render_capacity_update_form(self.database, st.session_state.user)

    def display_capacity_chart(self):
        if not PLOTLY_AVAILABLE:
            return
        capacities = self.database.get_all_bed_capacities()
        if capacities:
            data = []
            for c in capacities:
                if c.total_beds > 0:
                    short_name = c.hospital_name.split('(')[0].strip()[:30]
                    data.append({'Hospital': short_name, 'Occupied': c.occupied_beds,
                                 'Available': c.total_beds - c.occupied_beds, 'Total': c.total_beds})
            if data:
                df = pd.DataFrame(data)
                fig = px.bar(df, x='Hospital', y=['Occupied', 'Available'],
                             title='Bed Occupancy by Hospital',
                             color_discrete_map={'Occupied': '#E24B4A', 'Available': '#639922'}, barmode='stack')
                st.plotly_chart(fig, use_container_width=True, key="bed_chart")


# =============================================================================
# AUDIT LOG UI
# =============================================================================
class AuditLogUI:
    def __init__(self, database: Database):
        self.database = database

    def display(self):
        st.title("🔍 Audit Log — Kenya Data Protection Act 2019 Compliance")
        st.info("All user actions on patient data are logged here in compliance with Kenya's Data Protection Act 2019.")
        logs = self.database.get_audit_logs(limit=200)
        if logs:
            data = [{'Timestamp': l.timestamp.strftime('%Y-%m-%d %H:%M:%S'), 'User': l.user_id,
                     'Role': l.user_role, 'Action': l.action, 'Resource': l.resource_type,
                     'Record ID': l.resource_id, 'Details': l.details} for l in logs]
            df = pd.DataFrame(data)
            search = st.text_input("Filter logs (by user, action, resource)...")
            if search:
                mask = df.apply(lambda row: row.astype(str).str.contains(search, case=False).any(), axis=1)
                df = df[mask]
            st.dataframe(df, use_container_width=True)
            st.download_button("⬇️ Export Audit Log (CSV)", data=df.to_csv(index=False),
                               file_name=f"audit_log_{datetime.utcnow().strftime('%Y%m%d')}.csv", mime="text/csv")
        else:
            st.info("No audit records yet.")


# =============================================================================
# HANDOVER UI
# =============================================================================
class HandoverUI:
    def __init__(self, database: Database):
        self.database = database
        self.moh_service = MOHReferralLetterService()

    def display(self):
        st.title("📄 Patient Handover Management")
        tab1, tab2 = st.tabs(["Create Handover Form", "Handover History"])
        with tab1: self.create_handover_form()
        with tab2: self.display_handover_history()

    def create_handover_form(self):
        st.subheader("Create Handover Form")
        patients = self.database.get_all_patients()
        user_hospital = st.session_state.user['hospital']
        if user_hospital == "All Facilities":
            eligible = [p for p in patients if p.status == 'Arrived at Destination']
        else:
            eligible = [p for p in patients if p.receiving_hospital == user_hospital
                        and p.status == 'Arrived at Destination']

        if not eligible:
            st.info("No patients eligible for handover (must have status 'Arrived at Destination')")
            return

        patient_options = {f"{p.patient_id} - {p.name}": p for p in eligible}
        selected_key = st.selectbox("Select Patient", list(patient_options.keys()))
        selected_patient = patient_options[selected_key]

        with st.form("handover_form", clear_on_submit=True):
            st.write(f"**Patient:** {selected_patient.name}")
            st.write(f"**Condition:** {selected_patient.condition}")
            triage_icon = {'Red': '🔴', 'Orange': '🟠', 'Green': '🟢'}.get(selected_patient.triage_level, '⚪')
            st.write(f"**Triage:** {triage_icon} {selected_patient.triage_level or 'N/A'} "
                     f"(MEWS: {selected_patient.mews_score or 0})")
            st.write(f"**SHA Claim:** {selected_patient.sha_claim_id or 'None'}")

            st.subheader("Vital Signs at Handover")
            col1, col2 = st.columns(2)
            with col1:
                blood_pressure = st.text_input("Blood Pressure", value="120/80")
                heart_rate = st.number_input("Heart Rate (bpm)", min_value=0, max_value=200, value=72)
            with col2:
                temperature = st.number_input("Temperature (°C)", min_value=30.0, max_value=45.0, value=36.6)
                oxygen_saturation = st.number_input("Oxygen Saturation (%)", min_value=0, max_value=100, value=98)

            st.subheader("Handover Details")
            receiving_physician = st.text_input("Receiving Physician*")
            handover_notes = st.text_area("Handover Notes")

            submitted = st.form_submit_button("Complete Handover", use_container_width=True)
            if submitted:
                if not receiving_physician:
                    st.error("Please enter the receiving physician")
                else:
                    self.database.add_handover_form({
                        'patient_id': selected_patient.patient_id,
                        'patient_name': selected_patient.name, 'age': selected_patient.age,
                        'condition': selected_patient.condition,
                        'referring_hospital': selected_patient.referring_hospital,
                        'receiving_hospital': selected_patient.receiving_hospital,
                        'referring_physician': selected_patient.referring_physician,
                        'receiving_physician': receiving_physician,
                        'vital_signs': {'blood_pressure': blood_pressure, 'heart_rate': heart_rate,
                                        'temperature': temperature, 'oxygen_saturation': oxygen_saturation},
                        'medical_history': selected_patient.medical_history,
                        'current_medications': selected_patient.current_medications,
                        'allergies': selected_patient.allergies, 'notes': handover_notes,
                        'ambulance_id': selected_patient.assigned_ambulance,
                        'created_by': st.session_state.user['role'],
                    })
                    Patient.objects.filter(patient_id=selected_patient.patient_id).update(
                        status='Completed', receiving_physician=receiving_physician
                    )
                    if selected_patient.sha_claim_id:
                        SHAClaim.objects.filter(claim_id=selected_patient.sha_claim_id).update(
                            status='Approved', approved_at=datetime.utcnow()
                        )
                        Patient.objects.filter(patient_id=selected_patient.patient_id).update(sha_claim_status='Approved')
                    self.database.log_action(st.session_state.user['role'], st.session_state.user['role'],
                                             'COMPLETE_HANDOVER', 'Patient', selected_patient.patient_id,
                                             'Handover completed')
                    st.success("Handover completed successfully!")
                    st.balloons()

                    if REPORTLAB_AVAILABLE:
                        try:
                            selected_patient.refresh_from_db()
                            ref_row = hospitals_df[hospitals_df['facility_name'] == selected_patient.referring_hospital].iloc[0]
                            rec_row = hospitals_df[hospitals_df['facility_name'] == selected_patient.receiving_hospital].iloc[0]
                            pdf_bytes = self.moh_service.generate_moh_referral_letter(
                                selected_patient, ref_row.to_dict(), rec_row.to_dict()
                            )
                            if pdf_bytes:
                                st.download_button("⬇️ Download Completed MOH Referral Letter", data=pdf_bytes,
                                                   file_name=f"MOH_Completed_{selected_patient.patient_id}.pdf",
                                                   mime="application/pdf",
                                                   key=f"moh_handover_{selected_patient.patient_id}")
                        except Exception as e:
                            st.warning(f"Could not generate final letter: {e}")

    def display_handover_history(self):
        st.subheader("Handover History")
        user_hospital = st.session_state.user['hospital']
        handovers = list(HandoverForm.objects.all() if user_hospital == "All Facilities"
                         else HandoverForm.objects.filter(receiving_hospital=user_hospital))
        if handovers:
            for h in handovers:
                with st.expander(f"{h.patient_name} — {h.transfer_time.strftime('%Y-%m-%d %H:%M')}"):
                    col1, col2 = st.columns(2)
                    with col1:
                        st.write(f"**Patient ID:** {h.patient_id}")
                        st.write(f"**Age:** {h.age}")
                        st.write(f"**Condition:** {h.condition}")
                        st.write(f"**Referring Hospital:** {h.referring_hospital}")
                        st.write(f"**Receiving Hospital:** {h.receiving_hospital}")
                    with col2:
                        st.write(f"**Referring Physician:** {h.referring_physician}")
                        st.write(f"**Receiving Physician:** {h.receiving_physician}")
                        st.write(f"**Ambulance:** {h.ambulance_id}")
                        st.write(f"**Handover Time:** {h.transfer_time.strftime('%Y-%m-%d %H:%M')}")
                    if h.vital_signs:
                        vs = h.vital_signs
                        c1, c2, c3, c4 = st.columns(4)
                        with c1: st.metric("BP", vs.get('blood_pressure', 'N/A'))
                        with c2: st.metric("HR", f"{vs.get('heart_rate', 'N/A')} bpm")
                        with c3: st.metric("Temp", f"{vs.get('temperature', 'N/A')}°C")
                        with c4: st.metric("SpO₂", f"{vs.get('oxygen_saturation', 'N/A')}%")
                    if h.notes:
                        st.write(f"**Handover Notes:** {h.notes}")
        else:
            st.info("No handover forms completed")


# =============================================================================
# COMMUNICATION UI
# =============================================================================
class CommunicationUI:
    def __init__(self, database: Database, notification_service: NotificationService):
        self.database = database
        self.notification_service = notification_service

    def display(self):
        st.title("💬 Communication Center")
        tab1, tab2, tab3 = st.tabs(["Send Notifications", "Message Templates", "Communication Log"])
        with tab1: self.send_notifications()
        with tab2: self.message_templates()
        with tab3: self.communication_log()

    def send_notifications(self):
        st.subheader("Send Notifications")
        with st.form("notification_form"):
            notification_type = st.selectbox("Notification Type",
                ["Referral Alert", "Ambulance Dispatch", "Patient Arrival", "Emergency", "General Update"])
            recipient_type = st.radio("Recipient", ["Hospital", "Ambulance Driver", "Specific Contact"])
            if recipient_type == "Hospital":
                recipient = st.selectbox("Select Hospital", [
                    "Jaramogi Oginga Odinga Teaching & Referral Hospital (JOOTRH)",
                    "Kisumu County Referral Hospital", "Lumumba Sub-County Hospital", "All Hospitals",
                ])
            elif recipient_type == "Ambulance Driver":
                ambulances = self.database.get_all_ambulances()
                recipient = st.selectbox("Select Driver",
                    [f"{a.ambulance_id} - {a.driver_name}" for a in ambulances])
            else:
                recipient = st.text_input("Contact Number/Email")
            subject = st.text_input("Subject", value=f"{notification_type} Notification")
            message = st.text_area("Message", height=150)
            col1, col2, col3 = st.columns(3)
            with col1: send_sms = st.checkbox("Send SMS", value=True)
            with col2: send_email_chk = st.checkbox("Send Email")
            with col3: urgent = st.checkbox("Urgent", value=False)
            submitted = st.form_submit_button("Send Notification", use_container_width=True)
            if submitted:
                if not message:
                    st.error("Please enter a message")
                else:
                    if send_sms: st.success("📱 SMS notification prepared for sending")
                    if send_email_chk: st.success("📧 Email notification prepared for sending")
                    if urgent: st.warning("🚨 URGENT notification marked")
                    st.info(f"Notification will be sent to: {recipient}")

    def message_templates(self):
        st.subheader("Message Templates")
        templates = {
            "New Referral": "New patient referral received: {patient_name} with {condition}. Triage: {triage}.",
            "Ambulance Dispatch": "Ambulance {ambulance_id} dispatched for patient {patient_name}. ETA: {eta} minutes.",
            "Patient Arrival": "Patient {patient_name} has arrived at {hospital}. Condition: {condition}.",
            "Emergency": "EMERGENCY: {message}. Immediate response required.",
            "Status Update": "Patient {patient_name} status update: {status}. Location: {location}.",
        }
        selected = st.selectbox("Select Template", list(templates.keys()))
        st.text_area("Template Content", templates[selected], height=100)
        if st.button("Save as New Template", use_container_width=True):
            st.success("Template saved successfully!")

    def communication_log(self):
        st.subheader("Communication Log")
        comms = list(Communication.objects.order_by('-timestamp')[:50])
        if comms:
            for comm in comms:
                with st.expander(f"{comm.timestamp.strftime('%Y-%m-%d %H:%M')} — "
                                 f"{comm.message_type} to {comm.receiver}"):
                    st.write(f"**From:** {comm.sender}")
                    st.write(f"**Message:** {comm.message}")
                    st.write(f"**Patient ID:** {comm.patient_id}")
        else:
            st.info("No communications logged yet.")


# =============================================================================
# REPORTS UI
# =============================================================================
class ReportsUI:
    def __init__(self, database: Database, analytics: AnalyticsService):
        self.database = database
        self.analytics = analytics

    def display(self):
        st.title("📈 Reports & Analytics")
        tab1, tab2, tab3, tab4, tab5 = st.tabs(
            ["Performance Metrics", "Hospital Analytics", "Ambulance Reports", "SHA Billing Report", "Export Data"])
        with tab1: self.performance_metrics()
        with tab2: self.hospital_analytics()
        with tab3: self.ambulance_reports()
        with tab4: self.sha_billing_report()
        with tab5: self.export_data()

    def performance_metrics(self):
        st.subheader("Performance Metrics")
        kpis = self.analytics.get_kpis()
        col1, col2, col3, col4 = st.columns(4)
        with col1: st.metric("Total Referrals", kpis['total_referrals'])
        with col2: st.metric("Completion Rate", kpis['completion_rate'])
        with col3: st.metric("Avg Response Time", kpis['avg_response_time'])
        with col4: st.metric("Active Transfers", kpis['active_referrals'])
        if PLOTLY_AVAILABLE:
            patients = self.database.get_all_patients()
            if patients:
                triage_counts = {'Red': 0, 'Orange': 0, 'Green': 0}
                for p in patients:
                    triage_counts[p.triage_level or 'Green'] = triage_counts.get(p.triage_level or 'Green', 0) + 1
                st.plotly_chart(px.pie(values=list(triage_counts.values()), names=list(triage_counts.keys()),
                                       title="Referrals by Triage Level",
                                       color_discrete_map={'Red': '#E24B4A', 'Orange': '#EF9F27', 'Green': '#639922'}),
                                use_container_width=True, key="triage_pie")
            trends = self.analytics.get_referral_trends()
            if not trends.empty:
                st.plotly_chart(px.line(trends, x='date', y='count', title="Daily Referral Trends"),
                                use_container_width=True, key="response_time_chart")

    def hospital_analytics(self):
        st.subheader("Hospital Performance")
        if not PLOTLY_AVAILABLE:
            st.info("Plotly not available.")
            return
        stats = self.analytics.get_hospital_stats()
        if not stats.empty:
            hospital_referrals = stats.groupby('hospital')['count'].sum().reset_index()
            st.plotly_chart(px.bar(hospital_referrals, x='hospital', y='count', title="Total Referrals by Hospital"),
                            use_container_width=True, key="hospital_referrals_chart")
        else:
            st.info("No hospital data available")

    def ambulance_reports(self):
        st.subheader("Ambulance Utilization")
        ambulances = self.database.get_all_ambulances()
        if ambulances:
            if PLOTLY_AVAILABLE:
                status_counts = {}
                for a in ambulances:
                    status_counts[a.status] = status_counts.get(a.status, 0) + 1
                st.plotly_chart(px.pie(values=list(status_counts.values()), names=list(status_counts.keys()),
                                       title="Ambulance Status Distribution"),
                                use_container_width=True, key="ambulance_status_pie_chart")
            st.dataframe(pd.DataFrame([{'Ambulance ID': a.ambulance_id, 'Driver': a.driver_name,
                                        'Status': a.status, 'Fuel %': f"{a.fuel_level:.1f}",
                                        'Current Patient': a.current_patient or 'None',
                                        'Location': a.current_location} for a in ambulances]),
                         use_container_width=True)
        else:
            st.info("No ambulance data available")

    def sha_billing_report(self):
        st.subheader("SHA / SHIF Billing Summary")
        claims = self.database.get_sha_claims()
        if claims:
            total = sum(c.total_amount for c in claims)
            approved = sum(c.total_amount for c in claims if c.status == 'Approved')
            pending = sum(c.total_amount for c in claims if c.status == 'Submitted')
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total Claims", len(claims))
            col2.metric("Total Billed (KSh)", f"{total:,.0f}")
            col3.metric("Approved (KSh)", f"{approved:,.0f}")
            col4.metric("Pending (KSh)", f"{pending:,.0f}")
            st.dataframe(pd.DataFrame([{'Claim ID': c.claim_id, 'Patient': c.patient_id,
                                        'Distance km': c.distance_km, 'Total KSh': c.total_amount,
                                        'Status': c.status, 'Date': c.submitted_at.strftime('%Y-%m-%d')}
                                       for c in claims]), use_container_width=True)
        else:
            st.info("No SHA claims yet.")

    def export_data(self):
        st.subheader("Data Export")
        col1, col2 = st.columns(2)
        with col1:
            st.download_button("📊 Export Referrals as CSV", data=self._export_referrals_csv(),
                               file_name=f"referrals_{datetime.now().strftime('%Y%m%d')}.csv",
                               mime="text/csv", use_container_width=True)
            st.download_button("🚑 Export Ambulance Data as CSV", data=self._export_ambulances_csv(),
                               file_name=f"ambulances_{datetime.now().strftime('%Y%m%d')}.csv",
                               mime="text/csv", use_container_width=True)
        with col2:
            st.download_button("🏛️ Export SHA Claims as CSV", data=self._export_sha_claims_csv(),
                               file_name=f"sha_claims_{datetime.now().strftime('%Y%m%d')}.csv",
                               mime="text/csv", use_container_width=True)

    def _export_referrals_csv(self):
        return pd.DataFrame([{'Patient ID': p.patient_id, 'Name': p.name, 'Age': p.age,
                               'Condition': p.condition, 'Triage': p.triage_level, 'MEWS': p.mews_score,
                               'Referring Hospital': p.referring_hospital, 'Receiving Hospital': p.receiving_hospital,
                               'Status': p.status, 'SHA Verified': p.sha_verified,
                               'SHA Claim': p.sha_claim_id, 'Billing KSh': p.sha_billing_amount_kes,
                               'MOH Ref': p.moh_referral_number, 'Referral Time': p.referral_time,
                               'Assigned Ambulance': p.assigned_ambulance}
                              for p in self.database.get_all_patients()]).to_csv(index=False)

    def _export_ambulances_csv(self):
        return pd.DataFrame([{'Ambulance ID': a.ambulance_id, 'Driver': a.driver_name,
                               'Contact': a.driver_contact, 'Status': a.status,
                               'Location': a.current_location, 'Fuel %': a.fuel_level,
                               'Current Patient': a.current_patient}
                              for a in self.database.get_all_ambulances()]).to_csv(index=False)

    def _export_sha_claims_csv(self):
        return pd.DataFrame([{'Claim ID': c.claim_id, 'Patient ID': c.patient_id, 'Ambulance': c.ambulance_id,
                               'Distance km': c.distance_km, 'Base KSh': c.base_charge,
                               'Additional KSh': c.additional_charge, 'Total KSh': c.total_amount,
                               'Status': c.status, 'Submitted': c.submitted_at}
                              for c in self.database.get_sha_claims()]).to_csv(index=False)


# =============================================================================
# DRIVER UI
# =============================================================================
class DriverUI:
    def __init__(self, database: Database, notification_service: NotificationService):
        self.database = database
        self.notification_service = notification_service
        self.location_simulator = LocationSimulator(database)

    def display_driver_dashboard(self):
        st.header("🚑 Ambulance Driver Dashboard")
        driver_name = st.session_state.user.get('name', st.session_state.user['role'])
        ambulance = Ambulance.objects.filter(driver_name=driver_name).first()
        if not ambulance:
            st.error("No ambulance assigned to you")
            return

        col1, col2, col3 = st.columns(3)
        with col1: st.metric("Ambulance ID", ambulance.ambulance_id)
        with col2: st.metric("Status", ambulance.status)
        with col3: st.metric("Location", ambulance.current_location)

        if ambulance.current_patient and ambulance.status == 'On Transfer':
            patient = self.database.get_patient_by_id(ambulance.current_patient)
            if patient:
                triage_icon = {'Red': '🔴', 'Orange': '🟠', 'Green': '🟢'}.get(patient.triage_level, '⚪')
                st.subheader(f"Current Mission — {triage_icon} {patient.triage_level} Triage")
                col1, col2 = st.columns(2)
                with col1:
                    st.write(f"**Patient:** {patient.name}")
                    st.write(f"**Condition:** {patient.condition}")
                    st.write(f"**From:** {patient.referring_hospital}")
                    st.write(f"**To:** {patient.receiving_hospital}")
                    st.write(f"**Status:** {patient.status}")
                    st.write(f"**SHA Claim:** {patient.sha_claim_id or 'None'}")
                with col2:
                    if ambulance.latitude and ambulance.longitude:
                        st.map(pd.DataFrame({'lat': [ambulance.latitude], 'lon': [ambulance.longitude]}))
                    st.subheader("📍 Update Location")
                    with st.form("location_update_form"):
                        new_lat = st.number_input("Latitude", value=ambulance.latitude or -0.0916)
                        new_lng = st.number_input("Longitude", value=ambulance.longitude or 34.7680)
                        location_name = st.text_input("Location Name",
                                                      value=ambulance.current_location or "En route")
                        if st.form_submit_button("Update Location", use_container_width=True):
                            if AmbulanceService(self.database).update_ambulance_location(
                                    ambulance.ambulance_id, new_lat, new_lng, location_name, patient.patient_id):
                                st.success("Location updated!")

                self.display_communication_panel(patient, ambulance)

                st.subheader("Mission Completion")
                if st.button("✅ Mark Patient Delivered", use_container_width=True, type="primary"):
                    self.complete_mission(ambulance, patient)

        elif ambulance.status == 'Available':
            st.info("Awaiting assignment...")
            available_patients = list(Patient.objects.filter(status='Referred', assigned_ambulance__isnull=True))
            if available_patients:
                st.subheader("Available Missions")
                for patient in available_patients:
                    triage_icon = {'Red': '🔴', 'Orange': '🟠', 'Green': '🟢'}.get(patient.triage_level, '⚪')
                    with st.expander(f"{triage_icon} Mission: {patient.name} — {patient.condition}"):
                        st.write(f"**From:** {patient.referring_hospital}")
                        st.write(f"**To:** {patient.receiving_hospital}")
                        st.write(f"**Triage:** {patient.triage_level} (MEWS: {patient.mews_score or 0})")
                        if st.button("Accept Mission", key=f"accept_{patient.patient_id}", use_container_width=True):
                            Ambulance.objects.filter(ambulance_id=ambulance.ambulance_id).update(
                                current_patient=patient.patient_id, status='On Transfer'
                            )
                            Patient.objects.filter(patient_id=patient.patient_id).update(
                                assigned_ambulance=ambulance.ambulance_id, status='Ambulance Dispatched'
                            )
                            ambulance.refresh_from_db()
                            patient.refresh_from_db()
                            if patient.referring_hospital_lat and patient.receiving_hospital_lat:
                                thread = threading.Thread(
                                    target=self.location_simulator.start_simulation,
                                    args=(ambulance.ambulance_id, patient.patient_id,
                                          ambulance.latitude, ambulance.longitude,
                                          patient.receiving_hospital_lat, patient.receiving_hospital_lng),
                                    daemon=True
                                )
                                thread.start()
                            st.success(f"Mission accepted! Assigned to patient {patient.name}")
                            st.rerun()

        self.quick_actions(ambulance)

    def display_communication_panel(self, patient: Patient, ambulance: Ambulance):
        st.subheader("💬 Communication")
        comms = self.database.get_communications_for_patient(patient.patient_id)
        if comms:
            for comm in comms[:5]:
                ts = comm.timestamp.strftime('%H:%M')
                if comm.sender == 'Driver':
                    st.markdown(f"**You** ({ts}): {comm.message}")
                else:
                    st.markdown(f"**{comm.sender}** ({ts}): {comm.message}")
        else:
            st.info("No messages yet")
        with st.form("message_form"):
            message = st.text_area("Type your message")
            recipient = st.selectbox("Send to",
                [patient.referring_hospital, patient.receiving_hospital, "Both Hospitals"])
            if st.form_submit_button("Send Message", use_container_width=True):
                if message:
                    hospitals_list = ([patient.referring_hospital, patient.receiving_hospital]
                                      if recipient == "Both Hospitals" else [recipient])
                    for hosp in hospitals_list:
                        self.database.add_communication({'patient_id': patient.patient_id,
                                                         'ambulance_id': ambulance.ambulance_id,
                                                         'sender': 'Driver', 'receiver': hosp,
                                                         'message': message, 'message_type': 'driver_hospital'})
                    st.success("Message sent!")
                    st.rerun()
                else:
                    st.error("Please enter a message")

    def complete_mission(self, ambulance: Ambulance, patient: Patient):
        Ambulance.objects.filter(ambulance_id=ambulance.ambulance_id).update(
            status='Available', current_patient=None, mission_complete=True
        )
        Patient.objects.filter(patient_id=patient.patient_id).update(status='Arrived at Destination')
        if patient.sha_claim_id:
            SHAClaim.objects.filter(claim_id=patient.sha_claim_id).update(
                status='Approved', approved_at=datetime.utcnow()
            )
        self.database.log_action('driver', 'Ambulance Driver', 'COMPLETE_MISSION', 'Patient',
                                 patient.patient_id, f"Patient delivered via {ambulance.ambulance_id}")
        arrival_msg = f"Patient {patient.name} has arrived via ambulance {ambulance.ambulance_id}"
        for hosp in [patient.referring_hospital, patient.receiving_hospital]:
            self.database.add_communication({'patient_id': patient.patient_id,
                                             'ambulance_id': ambulance.ambulance_id,
                                             'sender': 'Driver', 'receiver': hosp,
                                             'message': arrival_msg, 'message_type': 'arrival_notification'})
        self.notification_service.send_notification(patient.receiving_hospital, arrival_msg, 'arrival')
        st.success("Mission completed! Patient delivered successfully.")
        st.balloons()

    def quick_actions(self, ambulance: Ambulance):
        st.subheader("Quick Status Updates")
        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("🔄 Mark Available", use_container_width=True):
                Ambulance.objects.filter(ambulance_id=ambulance.ambulance_id).update(
                    status='Available', current_patient=None
                )
                st.success("Status updated to Available"); st.rerun()
        with col2:
            if st.button("⛑️ Mark On Break", use_container_width=True):
                Ambulance.objects.filter(ambulance_id=ambulance.ambulance_id).update(status='On Break')
                st.success("Status updated to On Break"); st.rerun()
        with col3:
            if st.button("🔧 Maintenance", use_container_width=True):
                Ambulance.objects.filter(ambulance_id=ambulance.ambulance_id).update(status='Maintenance')
                st.success("Status updated to Maintenance"); st.rerun()


# =============================================================================
# MAIN APPLICATION
# =============================================================================
class HospitalReferralApp:
    def __init__(self):
        # Run Django migrations / create tables
        Database.run_migrations()

        self.auth = Authentication()
        self.database = Database()
        initialize_sample_data()

        self.analytics = AnalyticsService(self.database)
        self.notifications = NotificationService(self.database)
        self.dashboard_ui = DashboardUI(self.database, self.analytics)
        self.referral_ui = ReferralUI(self.database, self.notifications)
        self.tracking_ui = TrackingUI(self.database)
        self.handover_ui = HandoverUI(self.database)
        self.communication_ui = CommunicationUI(self.database, self.notifications)
        self.reports_ui = ReportsUI(self.database, self.analytics)
        self.driver_ui = DriverUI(self.database, self.notifications)
        self.sha_ui = SHABillingUI(self.database)
        self.bed_ui = BedManagementUI(self.database)
        self.audit_ui = AuditLogUI(self.database)

        for key, default in [('authenticated', False), ('user', None),
                             ('simulation_running', False), ('sha_verified', False)]:
            if key not in st.session_state:
                st.session_state[key] = default

    def run(self):
        self.auth.setup_auth_ui()
        if st.session_state.get('authenticated'):
            self.render_main_app()
        else:
            self.render_login_page()

    def render_login_page(self):
        st.title("🏥 Kisumu County Hospital Referral System")
        st.markdown("""
        ## Welcome to the Hospital Referral & Ambulance Tracking System

        Please login using the sidebar to access the system.

        **Demo Credentials:**
        - Admin: `admin` / `admin123`
        - Hospital Staff (JOOTRH): `hospital_staff` / `staff123`
        - Hospital Staff (Kisumu County): `kisumu_staff` / `kisumu123`
        - Ambulance Driver: `driver` / `driver123`
        """)
        col1, col2, col3, col4 = st.columns(4)
        with col1: st.metric("Hospitals in Network", "40")
        with col2: st.metric("Ambulances", "20")
        with col3: st.metric("Coverage Area", "Kisumu County")
        with col4: st.metric("SHA Integration", "✅ Active")

        st.subheader("System Features")
        st.markdown("""
        | # | Feature | Status |
        |---|---------|--------|
        | 1 | MEWS Triage scoring (Red/Orange/Green) | ✅ Active |
        | 2 | SHA/SHIF billing integration (KSh 4,500 base + KSh 75/km) | ✅ Active |
        | 3 | Real-time hospital bed & capacity tracking | ✅ Active |
        | 4 | FHIR R4 / KenyaEMR / DHIS2 interoperability export | ✅ Active |
        | 5 | MOH-standard referral letter (MOH 367) with QR code | ✅ Active |
        """)

    def render_main_app(self):
        user_role = st.session_state.user['role']
        user_name = st.session_state.user.get('name', user_role)
        st.sidebar.markdown("---")
        st.sidebar.info(f"**Logged in as:** {user_name}\n\n**Role:** {user_role}\n\n"
                        f"**Hospital:** {st.session_state.user['hospital']}")
        if user_role == 'Admin':
            self.render_admin_interface()
        elif user_role == 'Hospital Staff':
            self.render_staff_interface()
        elif user_role == 'Ambulance Driver':
            self.render_driver_interface()
        st.markdown("---")
        st.markdown("**Kisumu County Hospital Referral System** | SHA Integrated • MOH Compliant • FHIR R4 Ready")

    def render_admin_interface(self):
        tabs = st.tabs(["📊 Dashboard", "📋 Referrals", "🚑 Tracking", "📄 Handovers",
                        "🏛️ SHA Billing", "🏥 Bed Management", "💬 Communication",
                        "📈 Reports", "🔍 Audit Log", "👥 User Management"])
        with tabs[0]: self.dashboard_ui.display()
        with tabs[1]: self.referral_ui.display()
        with tabs[2]: self.tracking_ui.display()
        with tabs[3]: self.handover_ui.display()
        with tabs[4]: self.sha_ui.display()
        with tabs[5]: self.bed_ui.display()
        with tabs[6]: self.communication_ui.display()
        with tabs[7]: self.reports_ui.display()
        with tabs[8]: self.audit_ui.display()
        with tabs[9]: self.render_user_management()

    def render_staff_interface(self):
        tabs = st.tabs(["📊 Dashboard", "📋 Referrals", "🚑 Tracking", "📄 Handovers",
                        "🏛️ SHA Billing", "🏥 Bed Management", "💬 Communication"])
        with tabs[0]: self.dashboard_ui.display()
        with tabs[1]: self.referral_ui.display()
        with tabs[2]: self.tracking_ui.display()
        with tabs[3]: self.handover_ui.display()
        with tabs[4]: self.sha_ui.display()
        with tabs[5]: self.bed_ui.display()
        with tabs[6]: self.communication_ui.display()

    def render_driver_interface(self):
        self.driver_ui.display_driver_dashboard()

    def render_user_management(self):
        if self.auth.require_auth(['Admin']):
            st.header("👥 User Management")
            col1, col2 = st.columns(2)
            with col1:
                st.subheader("Add New User")
                with st.form("add_user_form"):
                    username = st.text_input("Username")
                    password = st.text_input("Password", type="password")
                    email = st.text_input("Email")
                    role = st.selectbox("Role", ["Admin", "Hospital Staff", "Ambulance Driver"])
                    hospital = st.selectbox("Hospital",
                        ["All Facilities",
                         "Jaramogi Oginga Odinga Teaching & Referral Hospital (JOOTRH)",
                         "Kisumu County Referral Hospital"] + hospitals_data['facility_name'][2:])
                    if st.form_submit_button("Add User", use_container_width=True):
                        self.database.log_action('admin', 'Admin', 'CREATE_USER', 'User', username, f"Role: {role}")
                        st.success(f"User {username} added successfully")
            with col2:
                st.subheader("Current Users")
                st.dataframe([
                    {"Username": "admin", "Role": "Admin", "Hospital": "All Facilities"},
                    {"Username": "hospital_staff", "Role": "Hospital Staff", "Hospital": "JOOTRH"},
                    {"Username": "kisumu_staff", "Role": "Hospital Staff", "Hospital": "Kisumu County Referral Hospital"},
                    {"Username": "driver", "Role": "Ambulance Driver", "Hospital": "Ambulance Service"},
                ])


# =============================================================================
# ENTRY POINT
# =============================================================================
if __name__ == "__main__":
    st.set_page_config(
        page_title=Config.PAGE_TITLE,
        page_icon=Config.PAGE_ICON,
        layout=Config.LAYOUT,
        initial_sidebar_state="expanded",
    )
    st.markdown("""
    <style>
    .main-header { font-size: 2.5rem; color: #1f77b4; text-align: center; margin-bottom: 2rem; }
    .metric-card { background-color: #f0f2f6; padding: 1rem; border-radius: 10px; border-left: 5px solid #1f77b4; }
    .stButton button { width: 100%; }
    </style>
    """, unsafe_allow_html=True)
    app = HospitalReferralApp()
    app.run()
