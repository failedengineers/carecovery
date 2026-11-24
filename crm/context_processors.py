from .models import Notification


def workspace_context(request):
    support = {
        "support_phone_display": "+91 97173 24135",
        "support_phone_link": "+919717324135",
        "support_whatsapp_url": "https://wa.me/919717324135?text=Hello%20CareCovery%20Support%2C%20I%20need%20help%20with%20my%20account.%20Issue%3A%20",
    }
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return support
    notifications = Notification.objects.filter(user=request.user).order_by("-created_at")[:6]
    return {
        **support,
        "workspace": request.user.organization,
        "unread_notifications": Notification.objects.filter(user=request.user, is_read=False).count(),
        "recent_notifications": notifications,
    }
