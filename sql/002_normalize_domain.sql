-- Idempotent: fold historical sightline_scans.domain values to lowercase so
-- Blindbeanroasters.com and blindbeanroasters.com group together on read.
-- New writes go through fetch.http.domain(), which already lowercases.
UPDATE sightline_scans
   SET domain = LOWER(domain)
 WHERE domain <> LOWER(domain);
