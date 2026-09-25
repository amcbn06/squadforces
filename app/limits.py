"""Sizes and lifetimes that are policy, not code. Change them here."""

MAX_GROUPS_PER_USER = 5      # groups one (non-admin) user may own
MAX_GROUP_MEMBERS = 10       # ceiling for the member limit a group owner may set
DEFAULT_GROUP_MEMBERS = 10   # member limit given to a new group (and to existing groups)

INVITE_DEFAULT_DAYS = 7
INVITE_MAX_DAYS = 30
INVITE_MAX_USES = 100        # most uses one link can be given
