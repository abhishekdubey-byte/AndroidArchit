from fastapi.staticfiles import StaticFiles


class DashboardFiles(StaticFiles):
    """The dashboard's own HTML/CSS/JS: browsers must check for a newer version on every load.

    Without Cache-Control, Chrome guesses a freshness time from Last-Modified and can keep using an
    old files.css for hours after an update. "no-cache" makes it revalidate; the ETag keeps that a
    tiny 304 when nothing changed."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response
