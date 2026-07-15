CREATE TABLE IF NOT EXISTS app.widget_part (
    id serial PRIMARY KEY,
    widget_id integer NOT NULL REFERENCES app.widget(id)
);
