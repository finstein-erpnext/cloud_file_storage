"""Typed storage errors.

Contract #5 (`docs/research/content-consumers-contract.md` §5) is the reason this module
exists: `india_compliance`'s GST Return Log catches **only** ``FileNotFoundError`` and then
nulls its own file pointer. So exactly one error in this hierarchy is a
``FileNotFoundError`` — :class:`CloudObjectNotFound`, raised only when the object is
genuinely absent. Every transport, credential, throttling or configuration failure raises
something that is *not* a ``FileNotFoundError`` and is never converted into one, never
swallowed.
"""

from boto3.exceptions import Boto3Error
from botocore.exceptions import (
	BotoCoreError,
	ClientError,
	ConnectTimeoutError,
	EndpointConnectionError,
	ReadTimeoutError,
)
from botocore.exceptions import (
	ConnectionError as BotoConnectionError,
)

#: The only exceptions the engine may translate into this hierarchy. Anything else — a
#: frappe `QueryDeadlockError`, a programming error, a KeyboardInterrupt — propagates
#: untouched, because mistranslating it would turn a lock conflict or a bug into "the
#: object store is having a bad day" and let the caller degrade straight past it.
TRANSLATABLE_ERRORS = (ClientError, BotoCoreError, Boto3Error)


class CloudStorageError(Exception):
	"""Base class for every error raised by the storage layer."""


class CloudStorageConfigurationError(CloudStorageError):
	"""Settings are missing, contradictory, or name a bucket that does not exist."""


class CloudStorageTransportError(CloudStorageError):
	"""The object store could not be reached or failed transiently.

	Explicitly NOT a ``FileNotFoundError``: converting this would let a caller conclude
	the bytes are gone and destroy its own pointer (contract #5).
	"""


class CloudStorageUnavailableError(CloudStorageTransportError):
	"""The circuit breaker is open — S3 is presumed down and was not called (A30)."""


class CloudStoragePermissionError(CloudStorageError):
	"""Credentials were rejected or the principal lacks the required S3 permission."""


class CloudObjectNotFound(CloudStorageError, FileNotFoundError):
	"""The object is genuinely absent from the bucket. The only FileNotFoundError here."""


class CloudStorageIntegrityError(CloudStorageError):
	"""Bytes did not match the recorded checksum/size."""


class CloudStorageModeError(CloudStorageError):
	"""The requested operation is not permitted in the active operation mode."""


# S3 error codes that mean "the object/bucket is not there", vs "you may not", vs
# "something broke". Anything unrecognised is treated as transport (the safe direction:
# a transient error must never be mistaken for absence).
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound", "NoSuchVersion"}
_PERMISSION_CODES = {
	"403",
	"AccessDenied",
	"AllAccessDisabled",
	"InvalidAccessKeyId",
	"SignatureDoesNotMatch",
	"ExpiredToken",
	"InvalidToken",
	"AccountProblem",
}
_CONFIGURATION_CODES = {"NoSuchBucket", "InvalidBucketName", "PermanentRedirect", "KMS.NotFoundException"}


def error_code(exc: ClientError) -> str:
	response = getattr(exc, "response", None) or {}
	error = response.get("Error") or {}
	code = str(error.get("Code") or "")
	if not code:
		code = str((response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or "")
	return code


def translate_client_error(exc: Exception, *, context: str = "") -> CloudStorageError:
	"""Map a boto3/botocore exception onto the typed hierarchy.

	The returned exception is raised by the caller (``raise translate_client_error(e) from e``)
	so the original traceback is preserved.
	"""
	suffix = f" ({context})" if context else ""

	if isinstance(exc, ClientError):
		code = error_code(exc)
		message = f"{code or exc.__class__.__name__}{suffix}"
		if code in _NOT_FOUND_CODES:
			return CloudObjectNotFound(message)
		if code in _PERMISSION_CODES:
			return CloudStoragePermissionError(message)
		if code in _CONFIGURATION_CODES:
			return CloudStorageConfigurationError(message)
		return CloudStorageTransportError(message)

	if isinstance(
		exc,
		ConnectTimeoutError | ReadTimeoutError | EndpointConnectionError | BotoConnectionError,
	):
		return CloudStorageTransportError(f"{exc.__class__.__name__}{suffix}")

	if isinstance(exc, BotoCoreError):
		return CloudStorageTransportError(f"{exc.__class__.__name__}{suffix}")

	return CloudStorageTransportError(f"{exc.__class__.__name__}{suffix}")


def is_transport_failure(exc: Exception) -> bool:
	"""Whether this failure should count towards opening the circuit breaker (A30).

	Absence and permission failures are answers, not outages — they must not trip it.
	"""
	return isinstance(exc, CloudStorageTransportError) and not isinstance(exc, CloudStorageUnavailableError)
