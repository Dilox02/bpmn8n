from django import forms
from django.contrib import admin
from django.shortcuts import redirect

from .models import AuditEntry, Card, LLMProvider, MessageSubscription, Project, Settings, TransitionError, UserTask


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
        fields = ("name", "preset", "model", "base_url", "api_key", "timeout", "cost_per_1k_input", "cost_per_1k_output")
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

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if form.changed_data:
            AuditEntry.write("settings.change", obj, fields=form.changed_data)

    def save_formset(self, request, form, formset, change):
        """The providers added, changed (which fields: never a key's value) and deleted, in the audit log."""
        deleted = [(f.instance.pk, f.instance.name) for f in formset.deleted_forms if f.instance.pk]
        super().save_formset(request, form, formset, change)
        for provider in formset.new_objects:
            AuditEntry.write("llmprovider.add", provider, name=provider.name)
        for provider, fields in formset.changed_objects:
            AuditEntry.write("llmprovider.change", provider, name=provider.name, fields=fields)
        for pk, name in deleted:
            AuditEntry.write("llmprovider.delete", object_type="llmprovider", object_id=pk, name=name)


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


@admin.register(AuditEntry)
class AuditEntryAdmin(admin.ModelAdmin):
    """Read-only, for superusers too: nobody adds, changes or deletes an entry."""

    list_display = ("at", "actor", "action", "object_type", "object_id", "case_id", "ip")
    list_filter = ("action", "object_type")
    search_fields = ("actor", "case_id", "object_id")
    date_hierarchy = "at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
