"""Framework-independent API error. main.py converts it into a consistent JSON body:
{"detail": "<message>", "code": "<machine_code>"}"""


class ApiError(Exception):
    def __init__(self, status: int, detail: str, code: str = "error"):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code
