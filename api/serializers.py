from django.contrib.auth import get_user_model
from rest_framework import serializers

from coordination.models import ChainTransaction
from core.models import SystemConfig
from files.models import Chunk, FileVersion, Share, StoredFile, Transfer
from storage.models import StorageNode


class UserSerializer(serializers.ModelSerializer):
    eth_address = serializers.CharField(source="profile.eth_address", read_only=True, default=None)

    class Meta:
        model = get_user_model()
        fields = ["id", "username", "email", "eth_address", "is_staff"]


class RegisterSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField(required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, min_length=8)

    def validate_username(self, value):
        if get_user_model().objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("Username already taken.")
        return value


class ChunkSerializer(serializers.ModelSerializer):
    class Meta:
        model = Chunk
        fields = ["index", "sha256", "size"]


class VersionSerializer(serializers.ModelSerializer):
    author = serializers.CharField(source="author.username", default=None)

    class Meta:
        model = FileVersion
        fields = ["number", "size", "chunk_size", "chunk_count", "merkle_root", "manifest_hash", "author",
                  "tx_hash", "comment", "created_at"]


class ShareSerializer(serializers.ModelSerializer):
    user = serializers.CharField(source="user.username")

    class Meta:
        model = Share
        fields = ["user", "permission", "tx_hash", "created_at"]


class FileSerializer(serializers.ModelSerializer):
    owner = serializers.CharField(source="owner.username")
    size = serializers.IntegerField(read_only=True)
    permission = serializers.SerializerMethodField()

    class Meta:
        model = StoredFile
        fields = ["id", "short_id", "chain_id", "name", "owner", "mime_type", "size", "latest_version",
                  "coordinator", "on_chain", "permission", "created_at", "updated_at"]

    def get_permission(self, obj):
        return self.context.get("permissions", {}).get(obj.pk)


class FileDetailSerializer(FileSerializer):
    versions = VersionSerializer(many=True, read_only=True)
    shares = ShareSerializer(many=True, read_only=True)

    class Meta(FileSerializer.Meta):
        fields = FileSerializer.Meta.fields + ["versions", "shares"]


class ShareRequestSerializer(serializers.Serializer):
    username = serializers.CharField()
    permission = serializers.ChoiceField(choices=["read", "write"], default="read")


class UsernameSerializer(serializers.Serializer):
    username = serializers.CharField()


class UploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    comment = serializers.CharField(required=False, allow_blank=True, max_length=200)


class NodeSerializer(serializers.ModelSerializer):
    endpoint = serializers.CharField(read_only=True)

    class Meta:
        model = StorageNode
        fields = ["id", "node_id", "host", "port", "udp_port", "http_port", "endpoint", "is_active", "status",
                  "last_rtt_ms", "last_seen", "chunk_count", "bytes_used", "peers", "simulated_failure",
                  "latency_ms", "on_chain"]
        read_only_fields = ["status", "last_rtt_ms", "last_seen", "chunk_count", "bytes_used", "peers",
                            "simulated_failure", "latency_ms", "on_chain"]


class TransferSerializer(serializers.ModelSerializer):
    user = serializers.CharField(source="user.username", default=None)
    file = serializers.CharField(source="file.name", default=None)
    throughput_mbps = serializers.FloatField(read_only=True)

    class Meta:
        model = Transfer
        fields = ["id", "user", "file", "version_number", "direction", "mode", "status", "bytes", "chunks",
                  "deduplicated", "duration_ms", "throughput_mbps", "error", "created_at", "detail"]


class SystemSerializer(serializers.ModelSerializer):
    mode = serializers.CharField(read_only=True)

    class Meta:
        model = SystemConfig
        fields = ["mode", "blockchain_enabled", "chunk_size", "replication_factor", "auto_repair", "chain_id",
                  "node_registry_address", "file_registry_address", "file_sharing_address",
                  "contracts_deployed_at", "mode_changed_at"]
        read_only_fields = ["chain_id", "node_registry_address", "file_registry_address", "file_sharing_address",
                            "contracts_deployed_at", "mode_changed_at"]


class ChainTxSerializer(serializers.ModelSerializer):
    user = serializers.CharField(source="user.username", default=None)
    file = serializers.CharField(source="file.name", default=None)

    class Meta:
        model = ChainTransaction
        fields = ["tx_hash", "contract", "function", "sender", "user", "file", "block_number", "gas_used",
                  "latency_ms", "ok", "error", "created_at"]
