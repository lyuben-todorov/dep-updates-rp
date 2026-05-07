"""Pydantic models for the shared entry schema.

Mirrors schema/entry.schema.json. Update both when the schema changes.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class Ecosystem(str, Enum):
    cargo = "cargo"
    maven = "maven"
    pip = "pip"
    npm = "npm"


class UpdateCategory(str, Enum):
    breaking = "breaking"
    non_breaking = "non-breaking"
    fix_after_update = "fix-after-update"
    unreproducible = "unreproducible"


class AuthorType(str, Enum):
    human = "human"
    bot = "bot"


class BotType(str, Enum):
    dependabot = "dependabot"
    renovate = "renovate"
    snyk = "snyk"
    other = "other"


class VersionUpdateType(str, Enum):
    major = "major"
    minor = "minor"
    patch = "patch"
    other = "other"


class Scope(str, Enum):
    runtime = "runtime"
    dev = "dev"
    build = "build"
    test = "test"
    other = "other"


class TopFailureCategory(str, Enum):
    COMPILATION_FAILURE = "COMPILATION_FAILURE"
    TEST_FAILURE = "TEST_FAILURE"
    DEPENDENCY_RESOLUTION_FAILURE = "DEPENDENCY_RESOLUTION_FAILURE"
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
    OTHER = "OTHER"


class UnreproducibilityReason(str, Enum):
    pre_build_failed = "pre_build_failed"
    post_passed_when_expected_to_fail = "post_passed_when_expected_to_fail"
    post_failed_when_expected_to_pass = "post_failed_when_expected_to_pass"
    fix_did_not_restore = "fix_did_not_restore"
    external_service_required = "external_service_required"
    toolchain_unavailable = "toolchain_unavailable"
    flaky_tests = "flaky_tests"
    timeout = "timeout"
    network_required = "network_required"
    other = "other"


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: HttpUrl
    organisation: str
    name: str


class PR(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: HttpUrl
    number: int = Field(ge=1)
    author: str
    authorType: AuthorType
    botType: BotType | None = None
    merged: bool | None = None
    mergedAt: datetime | None = None


class Commits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pre: str = Field(pattern=r"^[a-f0-9]{7,40}$")
    post: str = Field(pattern=r"^[a-f0-9]{7,40}$")
    fix: str | None = Field(default=None, pattern=r"^[a-f0-9]{7,40}$")
    preAuthorType: AuthorType | None = None
    postAuthorType: AuthorType | None = None
    fixAuthorType: AuthorType | None = None


class Update(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dependencyName: str
    previousVersion: str
    newVersion: str
    versionUpdateType: VersionUpdateType
    scope: Scope | None = None


class FatImage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rustVersion: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    sourceDateEpoch: int = Field(ge=0)
    aptSnapshot: str = Field(pattern=r"^[0-9]{8}T[0-9]{6}Z$")
    debianRelease: str = Field(pattern=r"^(buster|bullseye|bookworm|trixie)$")
    expectedDigest: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")


class FingerprintFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0)


class EnvironmentFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str = Field(
        pattern=r"^linux/(amd64|arm64|386|arm/v6|arm/v7|ppc64le|s390x|riscv64)$",
        description="Container platform, e.g. linux/arm64.",
    )
    digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    files: list[FingerprintFile] = Field(min_length=1)
    rustcVersion: str | None = None
    packageCount: int | None = None


class ThinImages(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expectedPre: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    expectedPost: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    expectedFix: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")


class VerifiedOn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str
    host: str | None = None
    verifiedAt: datetime
    fingerprintMatch: bool
    fatImageDigestMatch: bool | None = None
    outcomeMatch: bool | None = None


class Reproduction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fatImage: FatImage
    buildFlags: list[str] = Field(min_length=1)
    environmentFingerprints: list[EnvironmentFingerprint] = Field(min_length=1)
    thinImages: ThinImages | None = None
    verifiedOn: list[VerifiedOn] = Field(default_factory=list)


class Failure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topCategory: TopFailureCategory
    subCategory: str | None = None
    errorCodes: list[str] = Field(default_factory=list)


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    id: str = Field(pattern=r"^(cargo|maven|pip|npm)-[a-f0-9]{7,40}$")
    schemaVersion: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    ecosystem: Ecosystem
    category: UpdateCategory
    project: Project
    pr: PR
    commits: Commits
    update: Update
    reproduction: Reproduction | None = None
    failure: Failure | None = None
    ecosystemMetadata: dict[str, Any] = Field(default_factory=dict)
    unreproducibilityReason: UnreproducibilityReason | None = None
