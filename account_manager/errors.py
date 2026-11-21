class UserError(Exception):
    """A fixed, safe message suitable for the host's result view."""


class ContractError(UserError):
    pass


class ApiError(UserError):
    def __init__(self, message, *, uncertain=False, status=None, category=None):
        super().__init__(message)
        self.uncertain = uncertain
        self.status = status
        self.category = category
