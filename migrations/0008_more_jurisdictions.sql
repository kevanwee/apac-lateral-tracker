-- 0008 — Jurisdictions the first real source register needed.
--
-- Added in response to an actual failure, not speculatively: syncing
-- config/sources.yaml raised `unknown jurisdiction code(s) LA, KH, MM` because
-- the Rajah & Tann Asia network covers Laos, Cambodia and Myanmar and the 0002
-- seed did not.
--
-- This is the cost of making jurisdictions a table rather than free text, and
-- it is the intended behaviour: an unknown market stops the sync loudly
-- instead of quietly storing a code no chart can group by. The fix is one
-- migration, which is the "add a region by configuration" path working.
--
-- The non-APAC codes below come from moves the seeded trade outlet actually
-- reported. They are not depth markets; they exist so a real move is not
-- silently stripped of its office.

INSERT INTO jurisdictions (code, name, region, is_depth_market) VALUES
    -- APAC network gaps
    ('LA', 'Laos',       'APAC', false),
    ('KH', 'Cambodia',   'APAC', false),
    ('MM', 'Myanmar',    'APAC', false),
    ('BN', 'Brunei',     'APAC', false),
    ('BD', 'Bangladesh', 'APAC', false),
    ('LK', 'Sri Lanka',  'APAC', false),
    ('PK', 'Pakistan',   'APAC', false),
    ('MO', 'Macau SAR',  'APAC', false),
    ('MN', 'Mongolia',   'APAC', false),
    -- Australian states not seeded in 0002
    ('AU-SA',  'South Australia',              'APAC', true),
    ('AU-ACT', 'Australian Capital Territory',  'APAC', true),
    ('AU-TAS', 'Tasmania',                      'APAC', true),
    ('AU-NT',  'Northern Territory',            'APAC', true),
    -- Markets the seeded trade press reports moves in
    ('BE', 'Belgium',        'EMEA',     false),
    ('DE', 'Germany',        'EMEA',     false),
    ('FR', 'France',         'EMEA',     false),
    ('NL', 'Netherlands',    'EMEA',     false),
    ('LU', 'Luxembourg',     'EMEA',     false),
    ('IE', 'Ireland',        'EMEA',     false),
    ('CH', 'Switzerland',    'EMEA',     false),
    ('ES', 'Spain',          'EMEA',     false),
    ('IT', 'Italy',          'EMEA',     false),
    ('GI', 'Gibraltar',      'EMEA',     false),
    ('SA', 'Saudi Arabia',   'EMEA',     false),
    ('QA', 'Qatar',          'EMEA',     false),
    ('ZA', 'South Africa',   'EMEA',     false),
    ('MX', 'Mexico',         'Americas', false),
    ('BR', 'Brazil',         'Americas', false),
    ('CA', 'Canada',         'Americas', false)
ON CONFLICT (code) DO NOTHING;
