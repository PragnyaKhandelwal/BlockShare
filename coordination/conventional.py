"""Blockchain OFF: Django and its relational database are the central coordinator."""
from __future__ import annotations

from django.db import transaction

from files.models import Share, StoredFile

from .base import PERM_NONE, PERM_OWNER, Coordinator, CoordinationError, timed


class ConventionalCoordinator(Coordinator):
    mode = "conventional"
    label = "Conventional (Django coordinator)"
    source = "Django database"

    @timed("register_file")
    def register_file(self, user, file, version):
        return None  # the database rows written by the service layer are the record

    @timed("add_version")
    def add_version(self, user, file, version):
        self.require(user, file, "write")

    @timed("share_file")
    def share_file(self, owner, file, grantee, permission):
        self.require(owner, file, PERM_OWNER)
        if grantee == file.owner:
            raise CoordinationError("The owner already has full access.")
        share, _ = Share.objects.update_or_create(
            file=file, user=grantee, defaults={"permission": permission, "granted_by": owner, "tx_hash": ""}
        )
        return share

    @timed("revoke_access")
    def revoke_access(self, owner, file, grantee):
        self.require(owner, file, PERM_OWNER)
        if not Share.objects.filter(file=file, user=grantee).delete()[0]:
            raise CoordinationError("That user has no access to revoke.")

    @timed("transfer_ownership")
    def transfer_ownership(self, owner, file, new_owner):
        self.require(owner, file, PERM_OWNER)
        if new_owner == owner:
            raise CoordinationError("You already own this file.")
        with transaction.atomic():
            Share.objects.filter(file=file, user=new_owner).delete()
            file.owner = new_owner
            file.save(update_fields=["owner", "updated_at"])

    @timed("delete_file")
    def delete_file(self, owner, file):
        self.require(owner, file, PERM_OWNER)
        file.deleted = True
        file.save(update_fields=["deleted", "updated_at"])

    @timed("check_permission")
    def check_permission(self, user, file):
        if file.deleted:
            return PERM_NONE
        if file.owner_id == user.id:
            return PERM_OWNER
        share = Share.objects.filter(file=file, user=user).first()
        return share.permission if share else PERM_NONE

    @timed("list_owned")
    def owned_files(self, user):
        return list(StoredFile.objects.filter(owner=user, deleted=False))

    @timed("list_shared")
    def shared_files(self, user):
        shares = Share.objects.filter(user=user, file__deleted=False).select_related("file", "file__owner")
        return [(s.file, s.permission) for s in shares]

    def file_state(self, file):
        return {
            "source": self.source,
            "owner": file.owner.username,
            "latest_version": file.latest_version,
            "grants": [(s.user.username, s.permission) for s in file.shares.select_related("user")],
        }

    @timed("get_root")
    def registered_root(self, file, version_number):
        v = file.versions.filter(number=version_number).first()
        if v is None:
            raise CoordinationError("Unknown version")
        return v.merkle_root
