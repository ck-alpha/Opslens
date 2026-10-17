# Third-party notices

OpsLens original application code is distributed under the [MIT License](LICENSE).
Third-party software remains under its own copyright and license terms.

## Lumen

Lumen is a runtime dependency providing HoloViz agent, retrieval and tool abstractions;
those framework capabilities are not original OpsLens implementations.

- Source: <https://github.com/holoviz/lumen>
- Runtime commit: `c46a41aec7effba8ada8c41577fcc6432e7eeff3`, as declared in `pyproject.toml`
- License: BSD-3-Clause

The complete license notice from this pinned Lumen distribution follows:

```text
Copyright (c) 2018-2020, HoloViz team (holoviz.org).
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

 * Redistributions of source code must retain the above copyright
   notice, this list of conditions and the following disclaimer.

 * Redistributions in binary form must reproduce the above copyright
   notice, this list of conditions and the following disclaimer in the
   documentation and/or other materials provided with the
   distribution.

 * Neither the name of the copyright holder nor the names of any
   contributors may be used to endorse or promote products derived
   from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Other direct runtime dependencies

| Dependency / maintainers | License | Source |
| --- | --- | --- |
| Panel / HoloViz team | BSD-3-Clause | <https://github.com/holoviz/panel> |
| HTTPX / Encode and contributors | BSD-3-Clause | <https://github.com/encode/httpx> |
| DuckDB / DuckDB Foundation and contributors | MIT | <https://github.com/duckdb/duckdb-python> |
| SQLGlot contributors | MIT | <https://github.com/tobymao/sqlglot> |
| Pydantic contributors | MIT | <https://github.com/pydantic/pydantic> |
| SQLAlchemy / Michael Bayer and contributors | MIT | <https://github.com/sqlalchemy/sqlalchemy> |
| Alembic / Michael Bayer and contributors | MIT | <https://github.com/sqlalchemy/alembic> |
| Psycopg and psycopg-binary contributors | LGPL-3.0-only | <https://github.com/psycopg/psycopg> |

Dependency requirements are recorded in `pyproject.toml`. The installed distributions
include their own license files and notices, which also apply when redistributing an
environment or container. Transitive dependencies and libraries bundled with binary
packages retain their respective licenses; this table is not a replacement for those
notices. PostgreSQL uses the PostgreSQL License; its container image also includes
separately licensed system packages.
