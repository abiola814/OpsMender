from . import auth, dashboard, incidents, resources, servers, settings, webhooks

ROUTERS = [auth.router, dashboard.router, servers.router, resources.router, incidents.router, settings.router,
           webhooks.router]
