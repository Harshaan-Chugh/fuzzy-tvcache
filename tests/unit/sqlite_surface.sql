-- SQLite surface corpus for test_sql_properties.py: one statement per line after the fixture.
-- fixture
CREATE TABLE t(id INTEGER PRIMARY KEY, a, b, s TEXT, d TEXT, j TEXT, f REAL, x BLOB);
INSERT INTO t VALUES (1, 1, 2, 'Ab', '2024-01-31', '{"a":1,"b":[1,2]}', 1.5, x'41'),
 (2, '1.5', NULL, 'ab', '2024-02-29 13:45:00', '{"a":"x"}', -0.0, NULL),
 (3, NULL, 'b', NULL, 'bad', '[]', 1e300, x''), (4, 9223372036854775807, 3, 'a_b%', '2024-12-31', 'null', NULL, 'txt');
CREATE TABLE t2(id INTEGER, a); INSERT INTO t2 VALUES (1, 1), (2, NULL), (5, 'Ab');
-- statements
SELECT CAST(a AS BOOLEAN), CAST(a AS INT), CAST(a AS INTEGER), CAST(a AS FLOAT), CAST(a AS REAL), CAST(a AS DOUBLE) FROM t
SELECT CAST(a AS NUMERIC), CAST(a AS DECIMAL(10,2)), CAST(a AS VARCHAR(3)), CAST(a AS TEXT), CAST(a AS BLOB), CAST(a AS CHAR) FROM t
SELECT CAST(a AS DATE), CAST(a AS DATETIME), CAST(a AS TIMESTAMP), CAST(s AS BIGINT), CAST(f AS TINYINT), CAST(a AS STRING) FROM t
SELECT strftime('%Y', d), strftime('%m-%d', d), strftime('%s', d), strftime('%W', d), strftime('%j', d) FROM t
SELECT date(d), date(d, '+1 day'), date(d, 'start of month'), datetime(d), time(d), julianday(d) FROM t
SELECT julianday(d) - julianday('2024-01-01'), date(d, '-1 month'), datetime(d, 'unixepoch'), unixepoch(d) FROM t
SELECT json_extract(j, '$.a'), j -> '$.a', j ->> '$.a', json_array_length(j, '$.b'), json_type(j), json(j) FROM t
SELECT json_object('k', a), json_array(a, b), json_valid(j), json_extract(j, '$.b[0]') FROM t
SELECT max(a, b), min(a, b), max(a), min(b), greatest(a, b) FROM t
SELECT max(a, b), min(a, b) FROM t
SELECT substr(s, 2), substr(s, -2), substr(s, 1, 1), substring(s, 2), instr(s, 'b'), length(s), length(x), upper(s), lower(s) FROM t
SELECT trim(s, 'a'), ltrim(s, 'A'), rtrim(s), replace(s, 'b', 'c'), s || '!', s || a, hex(x), quote(s), unicode(s), char(65) FROM t
SELECT coalesce(a, b, 0), ifnull(a, 9), nullif(a, 1), iif(a, 'y', 'n'), CASE a WHEN 1 THEN 'one' ELSE 'other' END FROM t
SELECT typeof(a), typeof(f), typeof(x), a IS NULL, a ISNULL, a NOTNULL, a NOT NULL, a IS NOT NULL FROM t
SELECT a = 1, a == 1, a <> 1, a != 1, a IS 1, a IS NOT 1, a IS TRUE, a IS FALSE, a IN (1, 'b'), a NOT IN (2) FROM t
SELECT s LIKE 'a%', s LIKE 'A%', s GLOB 'a*', s GLOB 'A*', s NOT LIKE '%b', s LIKE 'a\_b\%' ESCAPE '\' FROM t
SELECT 7 / 2, 7.0 / 2, 7 / 2.0, 7 % 3, -7 % 3, 1 << 3, 8 >> 1, 5 & 3, 5 | 3, ~5, - -1, +a FROM t
SELECT 9223372036854775807 + 1, 9223372036854775808, 1e3, 1.0e-3, .5, 5., 0x10, 0xFF, 1000000000000000000000
SELECT 'it''s', "a", "nonexistent", 'a' || "b" || 1, x'00ff', X'41', NULL, TRUE, FALSE
SELECT round(f), round(f, 1), round(2.5), round(-2.5), abs(a), sign(a), ceil(f), floor(f), sqrt(4), power(2, 3), ln(1), log(100), log10(100) FROM t
SELECT count(*), count(a), count(DISTINCT a), sum(a), total(a), avg(a), group_concat(s), group_concat(s, ';'), string_agg(s, ',') FROM t
SELECT a, count(*) FROM t GROUP BY a HAVING count(*) >= 1 ORDER BY a NULLS LAST
SELECT a FROM t ORDER BY a NULLS FIRST
SELECT a FROM t ORDER BY a DESC NULLS FIRST
SELECT s FROM t ORDER BY s COLLATE NOCASE, id
SELECT s FROM t WHERE s = 'ab' COLLATE NOCASE
SELECT row_number() OVER (ORDER BY id DESC), rank() OVER (PARTITION BY b ORDER BY a), sum(a) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND CURRENT ROW), lag(a) OVER w, lead(a, 1, 0) OVER w FROM t WINDOW w AS (ORDER BY id)
SELECT ntile(2) OVER (ORDER BY id), first_value(a) OVER (ORDER BY id), last_value(a) OVER (ORDER BY id RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING), percent_rank() OVER (ORDER BY id), cume_dist() OVER (ORDER BY id) FROM t
SELECT a FROM t UNION SELECT a FROM t2
SELECT a FROM t UNION ALL SELECT a FROM t2 ORDER BY 1 LIMIT 3
SELECT a FROM t INTERSECT SELECT a FROM t2
SELECT a FROM t EXCEPT SELECT a FROM t2
SELECT * FROM t LIMIT 2 OFFSET 1
SELECT * FROM t LIMIT 1, 2
SELECT * FROM t LIMIT -1 OFFSET 2
SELECT * FROM t NATURAL JOIN t2
SELECT * FROM t JOIN t2 USING (id)
SELECT * FROM t LEFT JOIN t2 USING (id)
SELECT * FROM t LEFT OUTER JOIN t2 ON t.id = t2.id
SELECT * FROM t CROSS JOIN t2 LIMIT 3
SELECT * FROM t, t2 WHERE t.id = t2.id
SELECT * FROM t RIGHT JOIN t2 ON t.id = t2.id
SELECT * FROM t FULL OUTER JOIN t2 ON t.id = t2.id
SELECT * FROM t AS "U" WHERE "U".id = 1
SELECT u.id, v.id FROM t u, t v WHERE u.id = v.a
SELECT u.id FROM t u WHERE EXISTS (SELECT 1 FROM t2 v WHERE v.id = u.id)
SELECT (SELECT max(id) FROM t2), (SELECT count(*) FROM t WHERE a IS NULL)
SELECT * FROM (SELECT id, a FROM t) WHERE a > 0
SELECT * FROM (SELECT id, a FROM t) sub WHERE sub.a > 0
WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 5) SELECT * FROM n
WITH c AS (SELECT * FROM t), d AS (SELECT * FROM c WHERE id > 1) SELECT count(*) FROM d
WITH c(x) AS (VALUES (1), (2)) SELECT * FROM c
SELECT * FROM t WHERE rowid = 2
SELECT rowid, _rowid_, oid FROM t
SELECT * FROM t INDEXED BY sqlite_autoindex_t_1
SELECT * FROM t NOT INDEXED
SELECT DISTINCT b FROM t
SELECT ALL b FROM t
SELECT b FROM t WHERE a BETWEEN 0 AND 2 AND b NOT BETWEEN 5 AND 6
SELECT b FROM t WHERE a IN (SELECT a FROM t2) OR b IN ()
SELECT printf('%5.2f|%d|%s', f, a, s), format('%x', 255) FROM t
SELECT zeroblob(2), likelihood(a = 1, 0.5), unlikely(a), likely(a) FROM t
SELECT a FROM t WHERE a = 1.0
SELECT a FROM t WHERE s = "ab"
SELECT count(*) FILTER (WHERE a > 0) FROM t
SELECT s REGEXP 'a' FROM t
SELECT 1 WHERE 0
SELECT CURRENT_TIMESTAMP > '2000'
SELECT a FROM t ORDER BY 1 LIMIT (SELECT 2)
SELECT * FROM t2 WHERE a IS DISTINCT FROM 1
SELECT * FROM t2 WHERE a IS NOT DISTINCT FROM 1
SELECT a + b, a - b, a * b, a / b, -a FROM t
SELECT 'x' AS "col", a AS [b], b `c` FROM t
SELECT t.* FROM t
SELECT main.t.id FROM main.t
SELECT * FROM pragma_table_info('t')
SELECT * FROM json_each('[1,2]')
SELECT a FROM t WHERE (a, b) = (1, 2)
SELECT a FROM t WHERE a > ALL (SELECT 1)
SELECT a FROM t GROUP BY a ORDER BY count(*) DESC, a
SELECT iif(a > 1, 1, 0) + iif(b, 2, 0) FROM t
SELECT ifnull(s, '') || ifnull(d, '') FROM t
SELECT CAST(strftime('%Y', d) AS INTEGER) - 2000 FROM t
SELECT date('2024-01-31', '+1 month'), datetime('2024-01-01 00:00:00', '+90 minutes')
SELECT substr(d, 1, 4), substr(d, 6, 2) FROM t
SELECT a FROM t WHERE s LIKE '%' || 'b' || '%'
SELECT CASE WHEN a > 1 THEN 'big' WHEN a IS NULL THEN 'null' END FROM t
SELECT a FROM t WHERE NOT (a = 1 OR b = 2)
SELECT a FROM t WHERE a = 1 AND b = 2 OR s = 'ab'
SELECT avg(f), max(f), min(f), sum(f) FROM t
SELECT count(*) FROM t AS t1 JOIN t AS t2 ON t1.id < t2.id
SELECT group_concat(DISTINCT b) FROM t
SELECT group_concat(s ORDER BY s DESC) FROM t
INSERT INTO t2 VALUES (9, 'z') RETURNING *
INSERT INTO t2 (id, a) VALUES (1, 'up') ON CONFLICT DO NOTHING
INSERT INTO t (id, a) VALUES (1, 'up') ON CONFLICT (id) DO UPDATE SET a = excluded.a || 'x'
INSERT OR REPLACE INTO t (id, a) VALUES (1, 'r')
INSERT OR IGNORE INTO t (id) VALUES (1)
UPDATE t SET a = a + 1 WHERE id IN (SELECT id FROM t2) RETURNING id, a
UPDATE t SET (a, b) = (1, 2) WHERE id = 1
UPDATE OR IGNORE t SET id = 2 WHERE id = 1
UPDATE t SET a = t2.a FROM t2 WHERE t2.id = t.id
UPDATE t SET a = 1 ORDER BY id LIMIT 1
DELETE FROM t WHERE id = (SELECT max(id) FROM t2)
DELETE FROM t2
CREATE TABLE n (id INTEGER PRIMARY KEY, v TEXT NOT NULL DEFAULT 'x' CHECK (length(v) < 5), w REAL UNIQUE)
CREATE TABLE n (id INT, v VARCHAR(20), w BOOLEAN, d DATETIME, x FLOAT, PRIMARY KEY (id))
CREATE TABLE n (id) WITHOUT ROWID
CREATE TABLE n (id INTEGER PRIMARY KEY, x) STRICT
CREATE TABLE n (id INTEGER PRIMARY KEY AUTOINCREMENT, p INTEGER REFERENCES t(id))
CREATE UNIQUE INDEX ui ON t2 (id) WHERE id > 0
CREATE INDEX ix ON t (lower(s), a DESC)
ALTER TABLE t ADD COLUMN z INTEGER NOT NULL DEFAULT 0
ALTER TABLE t RENAME COLUMN a TO aa
ALTER TABLE t DROP COLUMN b
ALTER TABLE t2 RENAME TO t3
DROP TABLE t2
