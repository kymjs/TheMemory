from __future__ import annotations

from thememory.util import utc_now


class SystemClock:
    def now(self) -> str:
        return utc_now()
