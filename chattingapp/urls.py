"""
URL configuration for chattingapp project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, re_path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve as static_serve

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('login.urls')),
    path('chat/', include('chat.urls', namespace='chat')),
]

# Media serving: which route (if any) this deployment needs depends on
# WHERE the default storage writes files — not on INSTALLED_APPS.
_default_storage_backend = settings.STORAGES.get('default', {}).get('BACKEND', '')

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
elif _default_storage_backend == 'django.core.files.storage.FileSystemStorage':
    # Local-disk media in production (no Cloudinary configured) — serve it.
    urlpatterns += [
        re_path(r'^media/(?P<path>.*)$', static_serve, {'document_root': settings.MEDIA_ROOT}),
    ]
# else: media lives on Cloudinary; URLs point at res.cloudinary.com and no
# local /media/ route is needed.
