from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from storage.models import StorageNode


class RegisterForm(UserCreationForm):
    email = forms.EmailField(required=False)

    class Meta:
        model = User
        fields = ("username", "email")


class UploadForm(forms.Form):
    file = forms.FileField()
    comment = forms.CharField(max_length=200, required=False)


class ShareForm(forms.Form):
    username = forms.CharField(max_length=150)
    permission = forms.ChoiceField(choices=[("read", "Read"), ("write", "Read / Write")])


class UsernameForm(forms.Form):
    username = forms.CharField(max_length=150)


class NodeForm(forms.ModelForm):
    class Meta:
        model = StorageNode
        fields = ["node_id", "host", "port", "udp_port", "http_port"]


class SettingsForm(forms.Form):
    chunk_size_kb = forms.IntegerField(min_value=64, max_value=16384, label="Chunk size (KB)")
    replication_factor = forms.IntegerField(min_value=1, max_value=5)
    auto_repair = forms.BooleanField(required=False)
