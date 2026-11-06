"""Pydantic request models. Every body is validated here; unknown fields are rejected."""
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginIn(Strict):
    password: str = Field(min_length=1, max_length=256)
    totp: Optional[str] = Field(default=None, max_length=12)


class NoBody(Strict):
    pass


class ConfirmIn(Strict):
    confirm: bool = False


# ---- bots
class AddBotIn(Strict):
    token: str = Field(min_length=20, max_length=128)

    @field_validator("token")
    @classmethod
    def _shape(cls, v):
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", v):
            raise ValueError("does not look like a Telegram bot token")
        return v


# ---- files
class CaptionIn(Strict):
    caption: str = Field(default="", max_length=1024)


class DeleteFilesIn(Strict):
    tokens: list[str] = Field(min_length=1, max_length=500)
    mode: Literal["everywhere", "record"] = "everywhere"
    confirm: bool = False

    @field_validator("tokens")
    @classmethod
    def _tok(cls, v):
        for t in v:
            if not re.fullmatch(r"[A-Za-z0-9_-]{4,40}", t):
                raise ValueError("invalid file token")
        return list(dict.fromkeys(v))


# ---- posts
class FinalizePostIn(Strict):
    caption: Optional[str] = Field(default=None, max_length=1024)


class PostToChannelIn(Strict):
    confirm: bool = False


# ---- bulk
class BulkScanIn(Strict):
    start_link: str = Field(min_length=8, max_length=300)
    end_link: str = Field(min_length=8, max_length=300)
    mode: Literal["default", "original"] = "default"


class PublishBatchIn(Strict):
    to_channel: bool = True


# ---- copy chats
class PhoneIn(Strict):
    phone: str = Field(min_length=6, max_length=24)

    @field_validator("phone")
    @classmethod
    def _p(cls, v):
        v = v.replace(" ", "").replace("-", "")
        if not re.fullmatch(r"\+?\d{6,18}", v):
            raise ValueError("phone must be digits with country code, e.g. +919876543210")
        return v if v.startswith("+") else "+" + v


class CodeIn(Strict):
    code: str = Field(min_length=3, max_length=10)

    @field_validator("code")
    @classmethod
    def _c(cls, v):
        v = v.replace(" ", "").replace("-", "")
        if not v.isdigit():
            raise ValueError("code must be digits")
        return v


class PasswordIn(Strict):
    password: str = Field(min_length=1, max_length=256)


class ResolveIn(Strict):
    uid: int
    text: str = Field(min_length=1, max_length=200)


class CopyStartIn(Strict):
    uid: int
    src: int
    dest: int
    kinds: list[Literal["photo", "video", "file"]] = Field(default_factory=lambda: ["photo", "video", "file"], min_length=1)
    caption_only: bool = False
    resume: bool = True


class RemoveAccountIn(Strict):
    confirm: bool = False


# ---- auto posting
class ApRefIn(Strict):
    ref: str = Field(min_length=1, max_length=200)


class ApScheduleIn(Strict):
    interval_minutes: Optional[int] = Field(default=None, ge=1, le=525600)
    batch_size: Optional[int] = Field(default=None, ge=1, le=50)
    start_time: Optional[str] = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: Optional[str] = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    days: Optional[list[int]] = Field(default=None, min_length=1, max_length=7)
    jitter_minutes: Optional[int] = Field(default=None, ge=0, le=1440)

    @field_validator("days")
    @classmethod
    def _d(cls, v):
        if v is not None and any(d not in range(7) for d in v):
            raise ValueError("days must be 0-6 (0=Mon)")
        return sorted(set(v)) if v else v


class ApOptionsIn(Strict):
    loop: Optional[bool] = None
    dedupe: Optional[bool] = None
    notify_cycle: Optional[bool] = None
    auto_delete_minutes: Optional[Literal[0, 60, 360, 720, 1440, 2880, 10080]] = None
    footer: Optional[str] = Field(default=None, max_length=1000)   # "" removes the footer


class ApActiveIn(Strict):
    active: bool


class ApTextPostIn(Strict):
    text: str = Field(min_length=1, max_length=8000)


class ApBulkTextIn(Strict):
    filename: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=8_000_000)


class ApAlbumItem(Strict):
    media_type: Literal["photo", "video"]
    file_id: str = Field(min_length=5, max_length=400)
    file_unique_id: Optional[str] = Field(default=None, max_length=200)


class ApAlbumIn(Strict):
    items: list[ApAlbumItem] = Field(min_length=2, max_length=10)
    caption: Optional[str] = Field(default=None, max_length=1024)


class ApPromoIn(Strict):
    text: str = Field(min_length=1, max_length=4000)


class ApForceSubIn(Strict):
    ref: str = Field(min_length=1, max_length=200)    # "off" removes


class ApImportIn(Strict):
    first_link: str = Field(min_length=8, max_length=300)
    last_link: str = Field(min_length=8, max_length=300)


class ApDangerIn(Strict):
    confirm: bool = False
    admin_password: Optional[str] = Field(default=None, max_length=256)


# ---- ads / monetag
class AdsConfigIn(Strict):
    model_config = ConfigDict(extra="allow")


class RevenueIn(Strict):
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    network: str = Field(min_length=1, max_length=30)
    revenue_usd: float = Field(ge=0, le=1_000_000)


class MonetagConfirmedIn(Strict):
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    zone: str = Field(min_length=1, max_length=40)
    revenue_usd: float = Field(ge=0, le=1_000_000)
