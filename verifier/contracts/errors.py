"""Exception classes shared by every module (design doc §8.6)."""


class VerifierError(Exception): ...


class LLMError(VerifierError): ...


class LLMSchemaError(LLMError):
    """The reply didn't parse or validate. `.text` holds the raw reply."""

    def __init__(self, message: str, text: str):
        super().__init__(message)
        self.text = text


class ResumeError(VerifierError): ...


class SignatureError(VerifierError): ...


class FetchError(VerifierError): ...


class SandboxError(VerifierError): ...


class AnnotateError(VerifierError): ...


class SlackError(VerifierError): ...


class HarvestError(VerifierError): ...


class WriteOnceError(VerifierError): ...
