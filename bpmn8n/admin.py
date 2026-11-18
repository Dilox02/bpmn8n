from django import forms
from django.contrib import admin
from django.shortcuts import redirect

from .models import Card, LLMProvider, MessageSubscription, Project, Settings, TransitionError, UserTask


@admin.action(description="Cancel the selected waiting activities")
def cancel(modeladmin, request, queryset):
    cancelled = 0
    for activity in queryset:
        try:
            activity.cancel(f"cancelled by {request.user}")
            cancelled += 1
        except TransitionError:
            pass  # already completed or cancelled
    modeladmin.message_user(request, f"{cancelled} cancelled (the n8n executions are not stopped: stop them in n8n)")


@admin.register(UserTask)
class UserTaskAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "lane", "case_id", "status", "priority", "due", "assignee", "created", "completed")
    list_filter = ("status", "priority", "kind", "lane")
    search_fields = ("title", "case_id")
    actions = [cancel]


@admin.register(MessageSubscription)
class MessageSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("name", "correlation_key", "activity", "case_id", "status", "created", "completed")
    list_filter = ("status", "name")
    search_fields = ("name", "correlation_key", "case_id")
    actions = [cancel]


class LLMProviderForm(forms.ModelForm):
    class Meta:
        model = LLMProvider
        fields = ("name", "preset", "model", "base_url", "api_key", "timeout")
        widgets = {"api_key": forms.PasswordInput(render_value=True)}


class LLMProviderInline(admin.TabularInline):
    model = LLMProvider
    form = LLMProviderForm
    extra = 0


@admin.register(Settings)
class SettingsAdmin(admin.ModelAdmin):
    """One row: the list opens it, it cannot be added twice or deleted."""

    inlines = [LLMProviderInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        return redirect("admin:bpmn8n_settings_change", Settings.load().pk)


class CardInline(admin.TabularInline):
    model = Card
    fields = ("title", "status", "detail")
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "status", "created")
    list_filter = ("status",)
    readonly_fields = ("slug", "n8n_id")
    inlines = [CardInline]
