"""Column names that look like they hold a person's contact details.

Shared by evidence generation, which masks these values before describing a table, and value
indexing, which excludes them outright: a table small enough that every employee's email
looks "low cardinality" is exactly where that would otherwise go wrong.
"""

import re

PII_COLUMN = re.compile(
    r"e[-_]?mail|phone|fax|address|postal|zip|ssn|passport|"
    r"first[-_]?name|last[-_]?name|full[-_]?name|surname",
    re.IGNORECASE,
)
