"""Shared attendance transition contract; persistence remains in the existing stores."""
STATES={'OUT':'Logged Out','IN':'Working','ON_BREAK':'On Break','PENDING_AUTO_LOGOUT':'Pending Auto Logout','NEEDS_REVIEW':'Needs Review'}
ACTIONS={'OUT':['CHECK_IN'],'IN':['CHECK_OUT','START_BREAK'],'ON_BREAK':['END_BREAK','CHECK_OUT']}
NEXT={'CHECK_IN':'IN','CHECK_OUT':'OUT','START_BREAK':'ON_BREAK','END_BREAK':'IN'}


def session_state(session):
    if not session or session.get('exit_time') or not session.get('entry_confirmed'):
        return 'OUT'
    return 'ON_BREAK' if session.get('break_started_at') else 'IN'


def transition(state, action):
    if action not in ACTIONS.get(state,[]):
        raise ValueError('Attendance state changed; refresh before confirming')
    return NEXT[action]


def logout_update(session, stamp):
    """Checkout ends an active break at the checkout timestamp; no invented BREAK_OUT."""
    return stamp if session and session.get('break_started_at') else (session or {}).get('last_break_end')
