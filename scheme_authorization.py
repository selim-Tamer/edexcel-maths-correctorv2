"""Session-local capability boundary for future server-side scheme approval.

Only trusted application code may construct an authority. Its verifier must
recheck authentication, teacher authorization and assignment access on EVERY
call, returning a server-derived principal and login-session nonce. Neither
browser values nor an OAuth subject alone establish teacher authorization.
This module does not authenticate users or expose an endpoint. C6-B must wire
the verifier and revoke the authority at logout/account switch. No persistence.
"""
import unicodedata
from dataclasses import dataclass


class AuthorizationError(ValueError):
    """Content-free authorization failure."""


# C6-B1 F1: identifiers carry teacher/assignment identity across a security
# boundary, so any character that can visually spoof, hide or re-order text is
# refused: control (Cc), formatting/zero-width/bidi (Cf), surrogate (Cs),
# private-use (Co) and unassigned (Cn). Legitimate non-ASCII identities
# (accented Latin, CJK, Cyrillic, …) keep working because letters, marks and
# digits are unaffected. Note "unassigned" tracks the running Python's Unicode
# database, so a code point newly assigned in a future Unicode version becomes
# acceptable only when the runtime accepts it — a fail-closed direction.
_UNSAFE_IDENTIFIER_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def identifier(value):
    if (type(value) is not str or not 0 < len(value) <= 256
            or value != value.strip()):
        return False
    for char in value:
        if char.isspace() or ord(char) < 33 or ord(char) == 127:
            return False
        if unicodedata.category(char) in _UNSAFE_IDENTIFIER_CATEGORIES:
            return False
    return True


@dataclass(frozen=True)
class ServerIdentity:
    owner: str
    session_nonce: str
    assignment_revision: str | None = None
    # C6-B2 F-2: for a manual-review scope the verifier may additionally
    # vouch for ONE exact document (SHA-256 of bytes it independently owns).
    # A digest-bearing identity is only ever produced by the trusted verifier
    # when it could prove session-owned document provenance.
    document_digest: str | None = None


def _sha256_hex(value):
    """Strict lowercase SHA-256 hex check (no regex dependency)."""
    return (type(value) is str and len(value) == 64
            and all(char in "0123456789abcdef" for char in value))


@dataclass(frozen=True, eq=False, repr=False)
class AuthorizationContext:
    _authority: object
    _identity: ServerIdentity
    assignment_id: str | None


class ApprovalAuthority:
    """Bounded session registry; never serialize it into a review/browser.

    ``verify(assignment_id)`` returns ServerIdentity or None. None assignment
    means an explicitly authorized manual-review scope, not a fabricated ID.
    A fresh login must use a fresh nonce even for the same account.
    """

    def __init__(self, verify):
        if not callable(verify):
            raise AuthorizationError("trusted_verifier_required")
        self._verify = verify
        self._contexts = []
        self._reviews = {}
        self._revoked = False

    def _identity_for(self, assignment_id, document_digest=None):
        if self._revoked or (assignment_id is not None
                             and not identifier(assignment_id)):
            raise AuthorizationError("authorization_unavailable")
        if document_digest is not None:
            # C6-B2 F-2: a document digest only makes sense for the manual
            # (no fabricated assignment) scope, and only in strict form.
            if assignment_id is not None:
                raise AuthorizationError("document_provenance_scope_mismatch")
            if not _sha256_hex(document_digest):
                raise AuthorizationError("document_digest_required")
        try:
            if document_digest is None:
                identity = self._verify(assignment_id)
            else:
                identity = self._verify(assignment_id,
                                        document_digest=document_digest)
        except AuthorizationError:
            # C6-B1 F2: specific refusal codes from the trusted verifier
            # survive the boundary; only unexpected failures collapse to the
            # content-free generic code.
            raise
        except Exception:
            raise AuthorizationError("authorization_unavailable") from None
        if (type(identity) is not ServerIdentity or not identifier(identity.owner)
                or not identifier(identity.session_nonce)):
            raise AuthorizationError("authenticated_teacher_required")
        if assignment_id is not None and not identifier(identity.assignment_revision):
            raise AuthorizationError("verified_assignment_revision_required")
        if assignment_id is None and identity.assignment_revision is not None:
            raise AuthorizationError("manual_scope_revision_mismatch")
        if document_digest is not None and identity.document_digest != document_digest:
            # C6-B2 F-2: the verifier must independently prove THAT digest;
            # a caller-supplied digest alone proves nothing.
            raise AuthorizationError("document_provenance_unverified")
        return identity

    def _current_identity(self, assignment_id, document_digest=None):
        """Best-effort re-verification for pruning; None when unusable."""
        if self._revoked:
            return None
        try:
            identity = self._identity_for(assignment_id, document_digest)
        except AuthorizationError:
            return None
        except Exception:
            return None
        return identity if type(identity) is ServerIdentity else None

    def prune_superseded(self):
        """Bounded, fail-closed pruning of invalidated contexts and reviews (F3).

        C6-A never removed contexts or review entries short of revoke(), so a
        long-lived session that changed assignment revisions (or lost a grant)
        could accumulate stale entries until the hard 100-entry limits refused
        every new authorization — a permanent denial of approval for that
        session. Here, any entry whose stored ServerIdentity no longer equals
        a fresh verification for the same scope is dropped.

        Safety properties:
        - A pruned entry is removed permanently. ``validate``/``_bound_review``
          require registry membership, so a pruned context or review can never
          validate again — pruning can never revive or silently grant access,
          even if the old identity becomes reachable again later.
        - Only entries that CANNOT re-verify right now are evicted; currently
          valid entries are never touched.
        - The hard capacity limits in ``authorize``/``bind`` remain in place
          as the final fail-closed bound.
        """
        if self._revoked:
            return
        kept = [context for context in self._contexts
                if self._current_identity(context.assignment_id) == context._identity]
        self._contexts[:] = kept
        superseded = []
        for review_id, entry in self._reviews.items():
            binding = entry.get("binding") if type(entry) is dict else None
            assignment_id = (binding.get("assignment_id")
                             if type(binding) is dict else None)
            if type(entry) is not dict:
                superseded.append(review_id)
                continue
            if self._current_identity(assignment_id) != entry.get("identity"):
                superseded.append(review_id)
                continue
            if assignment_id is None:
                # C6-B2 F-2: a manual-scope binding additionally re-verifies
                # the EXACT document digest it was registered with; a grant
                # that was evicted (or a foreign/malformed digest) can no
                # longer re-verify, so the review entry is dropped. Removal is
                # permanent: a pruned entry can never be looked up again.
                digest = binding.get("document_digest")
                proven = (self._current_identity(None, document_digest=digest)
                          if _sha256_hex(digest) else None)
                if proven is None or proven.document_digest != digest:
                    superseded.append(review_id)
        for review_id in superseded:
            del self._reviews[review_id]

    def authorize(self, assignment_id):
        identity = self._identity_for(assignment_id)
        for context in self._contexts:
            if context._identity == identity and context.assignment_id == assignment_id:
                return context
        # F3: superseded contexts (owner/revision moved on, grant lost) can
        # neither validate nor persist; drop them so they cannot exhaust the
        # hard capacity below. Pruned means permanently invalid.
        self.prune_superseded()
        if len(self._contexts) >= 100:
            raise AuthorizationError("authorization_capacity_reached")
        context = AuthorizationContext(self, identity, assignment_id)
        self._contexts.append(context)
        return context

    def validate(self, context):
        if not any(item is context for item in self._contexts):
            raise AuthorizationError("issued_authorization_required")
        try:
            identity = self._identity_for(context.assignment_id)
        except AuthorizationError:
            # F3: the failure means this context can no longer re-verify;
            # drop every superseded entry so they cannot exhaust capacity.
            self.prune_superseded()
            raise
        if identity != context._identity:
            self.prune_superseded()
            raise AuthorizationError("session_or_owner_changed")
        return context._identity.owner

    def revoke(self):
        self._revoked = True
        self._contexts.clear()
        self._reviews.clear()


def validate_context(context):
    if (type(context) is not AuthorizationContext
            or type(context._authority) is not ApprovalAuthority):
        raise AuthorizationError("issued_authorization_required")
    return context._authority.validate(context)
