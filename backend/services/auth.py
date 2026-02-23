import secrets, hashlib, re
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from core.config import settings
from db.models import User, Organization, APIKey


from passlib.context import CryptContext
import hashlib

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

def hash_password(p):
    # Hash to sha256 first to avoid bcrypt 72-byte limit
    hashed = hashlib.sha256(p.encode()).hexdigest()
    return pwd_context.hash(hashed)

def verify_password(plain, hashed):
    plain_hashed = hashlib.sha256(plain.encode()).hexdigest()
    return pwd_context.verify(plain_hashed, hashed)


def create_access_token(data: dict, expires_delta=None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None

def generate_api_key():
    raw = f"aiq_sk_{secrets.token_urlsafe(32)}"
    h = hashlib.sha256(raw.encode()).hexdigest()
    prefix = raw[:16] + "..."
    return raw, h, prefix

def hash_api_key(raw): return hashlib.sha256(raw.encode()).hexdigest()

async def get_user_by_email(db, email):
    r = await db.execute(select(User).where(User.email == email))
    return r.scalar_one_or_none()

async def get_user_by_id(db, user_id):
    return await db.get(User, user_id)

async def authenticate_user(db, email, password):
    user = await get_user_by_email(db, email)
    if not user or not verify_password(password, user.hashed_password):
        return None
    return user if user.is_active else None

async def create_user(db, email, password, full_name, org_name):
    import secrets as _secrets
    from sqlalchemy import select as _select
    base_slug = re.sub(r"[^a-z0-9]", "-", org_name.lower())[:50]
    slug = base_slug
    # Ensure unique slug
    existing = await db.execute(_select(Organization).where(Organization.slug == slug))
    if existing.scalar_one_or_none():
        slug = f"{base_slug[:44]}-{_secrets.token_hex(3)}"
    org = Organization(name=org_name, slug=slug)
    db.add(org)
    await db.flush()
    user = User(email=email, hashed_password=hash_password(password),
                full_name=full_name, organization_id=org.id)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    await db.refresh(org)
    return user, org

async def get_org_by_api_key(db, raw_key):
    kh = hash_api_key(raw_key)
    r = await db.execute(select(APIKey).where(APIKey.key_hash == kh).where(APIKey.is_active == True))
    ak = r.scalar_one_or_none()
    if not ak:
        return None
    ak.last_used_at = datetime.utcnow()
    await db.commit()
    return await db.get(Organization, ak.organization_id)
