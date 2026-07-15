CREATE TABLE IF NOT EXISTS app.widget_part_detail (
    id serial PRIMARY KEY,
    widget_part_id integer NOT NULL REFERENCES app.widget_part(id)
);
