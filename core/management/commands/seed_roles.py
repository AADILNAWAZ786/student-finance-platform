"""Create the five actor groups (ARCHITECTURE §13); optionally one demo user per role.

Demo users get an unusable password (no password ever lives in the repo) and an API token,
printed once so the token can be used with ``Authorization: Token <key>``.
"""

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand
from django.db import transaction
from rest_framework.authtoken.models import Token

from core.permissions import ALL_ROLES


class Command(BaseCommand):
    help = (
        "Create role groups (learner, instructor, academic_admin, support_staff, platform_admin)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--demo-users",
            action="store_true",
            help="Also create demo_<role> users with API tokens (no passwords).",
        )

    @transaction.atomic
    def handle(self, *args, demo_users=False, **options):
        for role in ALL_ROLES:
            _, created = Group.objects.get_or_create(name=role)
            self.stdout.write(f"group {role}: {'created' if created else 'exists'}")
        if demo_users:
            for role in ALL_ROLES:
                user, created = User.objects.get_or_create(username=f"demo_{role}")
                if created:
                    user.set_unusable_password()
                    user.save()
                user.groups.add(Group.objects.get(name=role))
                token, _ = Token.objects.get_or_create(user=user)
                self.stdout.write(f"user {user.username}: token {token.key}")
