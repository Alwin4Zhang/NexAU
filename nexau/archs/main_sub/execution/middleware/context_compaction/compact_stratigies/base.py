# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Base protocol for compaction strategies."""

from typing import ClassVar, Protocol

from nexau.core.messages import Message


class CompactionStrategy(Protocol):
    """Protocol for compaction strategies that determine how to compact messages."""

    # RFC-0026: stable canonical name persisted into ``CompactAutoVariant.strategy``.
    # MUST be set by every concrete strategy class. Snake_case, kebab-free; this
    # value lands in ``nexau_agent_run_actions.extra->>'strategy'`` and downstream
    # views / dashboards / billing aggregate by it. Refactoring the Python class
    # name MUST NOT change this — picking a stable identifier here decouples
    # source-code identifiers from persisted data.
    name: ClassVar[str]

    def compact(
        self,
        messages: list[Message],
    ) -> list[Message]:
        """Compact messages to reduce token usage.

        Args:
            messages: Full message history

        Returns:
            Compacted message list
        """
        ...
