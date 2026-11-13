"""Role groups for the brief's actors (ARCHITECTURE.md §13).

Groups are created by ``seed_roles`` in a later phase; endpoints declare which
roles they accept with ``roles_required(...)``.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

LEARNER = "learner"
INSTRUCTOR = "instructor"
ACADEMIC_ADMIN = "academic_admin"
SUPPORT_STAFF = "support_staff"
PLATFORM_ADMIN = "platform_admin"
ALL_ROLES = (LEARNER, INSTRUCTOR, ACADEMIC_ADMIN, SUPPORT_STAFF, PLATFORM_ADMIN)


class HasAnyRole(BasePermission):
    """Allow authenticated users in any of ``roles`` (superusers always pass)."""

    roles: tuple[str, ...] = ()
    message = "Your role does not allow this action."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        return user.groups.filter(name__in=self.roles).exists()


class ReadWriteRoles(BasePermission):
    """Safe methods need a read role; anything else needs a write role (superusers pass).

    Views set ``read_roles`` and ``write_roles``.
    """

    message = "Your role does not allow this action."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        safe = request.method in ("GET", "HEAD", "OPTIONS")
        roles = getattr(view, "read_roles" if safe else "write_roles", ())
        return bool(roles) and user.groups.filter(name__in=roles).exists()


# Phase 1 access matrix (ARCHITECTURE §13). Learner "own record" and instructor "their
# programs" need a user↔student / user↔program link that the frozen model does not define;
# until it exists, learners have no access and instructors read only master data.
STAFF_READ = (PLATFORM_ADMIN, ACADEMIC_ADMIN, SUPPORT_STAFF)
MASTER_DATA_READ = (*STAFF_READ, INSTRUCTOR)
ACADEMIC_WRITE = (PLATFORM_ADMIN, ACADEMIC_ADMIN)

# Phase 2 billing matrix (decision D6, ARCHITECTURE §13): platform administrators manage
# billing; support staff read financial records. Academic administrators and instructors
# have no access to financial data; learners stay blocked until a user↔student link exists.
BILLING_READ = (PLATFORM_ADMIN, SUPPORT_STAFF)
BILLING_WRITE = (PLATFORM_ADMIN,)


def roles_required(*roles: str) -> type[HasAnyRole]:
    unknown = set(roles) - set(ALL_ROLES)
    if not roles or unknown:
        raise ValueError(f"roles must be a non-empty subset of {ALL_ROLES}; unknown: {unknown}")
    return type(f"HasAnyRole_{'_'.join(roles)}", (HasAnyRole,), {"roles": tuple(roles)})
