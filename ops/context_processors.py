from .models import Notification


def notifications(request):
    if not request.user.is_authenticated:
        return {}
    return {"unread_count": Notification.objects.filter(user=request.user, read_at__isnull=True).count()}
