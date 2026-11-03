from . import auth, dashboard, incidents, optimize, resources, servers, settings, webhooks

ROUTERS = [auth.router, dashboard.router, servers.router, resources.router, incidents.router, optimize.router,
           settings.router, webhooks.router]
