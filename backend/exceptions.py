"""Jobbunt application exceptions with consistent HTTP error responses."""


class JobbuntError(Exception):
    """Base application error."""
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class NotFoundError(JobbuntError):
    def __init__(self, resource: str, id=None):
        msg = f"{resource} not found" if id is None else f"{resource} {id} not found"
        super().__init__(msg, 404)


class ProfileAccessDenied(JobbuntError):
    def __init__(self):
        super().__init__("Profile does not belong to this user", 403)


class AIProviderError(JobbuntError):
    def __init__(self, provider: str, detail: str = ""):
        super().__init__(f"AI provider error ({provider}): {detail}", 502)


class RateLimitError(JobbuntError):
    def __init__(self, detail: str = "Too many requests"):
        super().__init__(detail, 429)


class ValidationError(JobbuntError):
    def __init__(self, detail: str):
        super().__init__(detail, 422)
