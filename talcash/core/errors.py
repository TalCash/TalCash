class DecodeError(ValueError):
    """The bytes do not form a well-formed object."""


class ValidationError(ValueError):
    """A well-formed object breaks a consensus rule.

    `code` is a short stable identifier (e.g. "bad-nonce") that nodes can log
    and send to peers; `detail` is a human-readable explanation.
    """

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)
