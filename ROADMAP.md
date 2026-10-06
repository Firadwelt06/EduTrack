# EduTrack Roadmap

## Product direction

EduTrack is being developed as a dependable, self-hosted school management
system for **one school per installation**. A school runs and administers its
own deployment. Multi-tenant SaaS, cross-school accounts, and shared hosting
are explicitly out of scope for this roadmap.

Version numbers describe progressively stronger readiness, not a promise that
the current application is suitable for handling real student records. A
release is complete only when its acceptance criteria are met and verified.

## Release plan

### 0.2 — Repository and data-safety foundation

**Focus:** Make a fresh checkout installable and safer to share.

- Use a real, installable dependency manifest and make setup instructions
  reproducible.
- Keep personal-looking student, teacher, and account records out of the
  install schema; provide schema structure only.
- Make the single-school scope and current readiness limits visible.

**Acceptance:** A clean Python environment can install declared dependencies;
the schema contains no example account or personal records; setup and scope
documentation agree with the repository.

### 0.3 — Correctness and security baseline (in progress)

**Focus:** Prove the current behavior before expanding it.

- Add automated tests for authentication, role/data access boundaries, grades,
  student import, CSRF, and important failure paths.
- Fix issues surfaced by those tests, including invalid or incomplete form
  submissions and inconsistent database transaction handling.
- Audit routes and queries for authorization checks and ensure sensitive
  student actions are covered by the audit trail.
- Document supported Python/MySQL versions and a safe local test setup.

**Progress:** Initial regression coverage is in place for route access, CSRF,
student CSV rejection and formula neutralization, login backoff, invalid
grades, teacher grade ownership and dashboard scoping, student course privacy,
valid student import, student change audit events, and the empty install
schema (including the audit table). The covered defects have been fixed.
Student imports use per-row savepoints so a failed audit write cannot leave
that row partially committed, and academic-year selection changes are atomic.
Tests currently use mocked database connections; live MySQL integration and
supported deployment checks remain outstanding. A Windows/Python 3.14 GitHub
Actions workflow is defined; it still needs a successful run in GitHub after
publication.

**Acceptance:** Automated checks run from a clean checkout; sensitive route
tests demonstrate both allowed and denied access; no known critical
authorization or data-integrity defect remains.

### 0.4 — Maintainable application and database changes

**Focus:** Make changes safer to build and upgrade.

- Separate application creation, routes, domain logic, configuration, and
  database access into clear modules while preserving current behavior.
- Introduce ordered, versioned database migrations; stop treating a database
  dump as the upgrade mechanism.
- Add consistent connection cleanup, transaction boundaries, and explicit
  error reporting.

**Acceptance:** A new database can be created and upgraded through migrations;
an existing installation has a documented, tested upgrade path.

### 0.5 — Complete single-school workflows

**Focus:** Make the essential school tasks reliable end to end.

- Complete and test student, teacher, course, enrollment, and grade workflows.
- Add validation and understandable recovery for imports and administrative
  changes.
- Add useful search, filtering, pagination, and exports without weakening
  role-based access.

**Acceptance:** Staff can complete documented everyday tasks without direct
database edits; failures do not leave partial or contradictory records.

### 0.6 — Accessible, coherent user experience

**Focus:** Make the product approachable and usable across devices.

- Establish consistent visual branding, layouts, navigation, and form behavior.
- Improve mobile layouts, keyboard operation, focus visibility, labels, and
  screen-reader semantics.
- Provide clear empty states, validation feedback, and confirmation for
  destructive actions.

**Acceptance:** Core workflows pass a documented keyboard and responsive
usability review, with no critical accessibility barriers.

### 0.7 — Operational readiness

**Focus:** Run the application predictably outside a developer workstation.

- Add a supported production WSGI deployment path and environment-based
  configuration, with debug mode off by default.
- Add health checks, structured logs, operational monitoring guidance, and
  documented secrets handling.
- Move scheduled backup work out of web-process startup; document off-site
  storage, retention, and restore procedures.
- Verify backups by regularly restoring into an isolated test database.

**Acceptance:** A documented deployment can be started, monitored, backed up,
and restored without relying on a developer workstation or manual code edits.

### 0.8 — Privacy and security operations

**Focus:** Set defensible practices for sensitive school information.

- Document collected data, purpose, access, retention, deletion, and incident
  response; confirm applicable legal obligations with the operating school.
- Review secret rotation, session management, dependency updates, audit-log
  protection, and least-privilege database permissions.
- Add security headers and deployment guidance appropriate to the supported
  hosting model.

**Acceptance:** The school operator has a reviewed data-handling and incident
response procedure; a security review has no unresolved critical/high findings.

### 0.9 — Pilot and release candidate

**Focus:** Validate the complete system with a controlled single-school pilot.

- Exercise migration, onboarding, backup/restore, and recovery procedures.
- Gather staff feedback and fix release-blocking usability and reliability
  issues.
- Run performance, accessibility, and independent security reviews against
  representative data and the documented deployment.

**Acceptance:** Pilot exit criteria are met, release-blocking findings are
closed, and operators approve the runbooks and recovery evidence.

### 1.0 — Stable single-school release

**Focus:** Ship a supportable release with clear limits and upgrade guarantees.

- Publish installation, administration, backup/restore, troubleshooting, and
  upgrade documentation.
- Tag a tested release with a supported configuration matrix and a documented
  security update process.
- State support boundaries and known limitations honestly.

**Acceptance:** A school operator who did not build the software can deploy,
administer, upgrade, and restore it using the published documentation; the
0.9 release gates remain satisfied.

## Explicit non-goals through 1.0

- Shared multi-school hosting or tenant isolation.
- Self-service public registration or billing.
- A claim of compliance with an education privacy law without jurisdiction-
  specific review and operational evidence.

Revisit these only after the single-school product has a stable operational
baseline and a separate architecture and privacy review.
