CREATE TABLE IF NOT EXISTS myapp.users (
    id BIGINT NOT NULL,
    email TEXT NOT NULL,
    status myapp.status,
    addr myapp.address,
    tags TEXT[],
    CONSTRAINT users_pkey PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS myapp.posts (
    id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    CONSTRAINT posts_pkey PRIMARY KEY (id),
    CONSTRAINT posts_user_fk FOREIGN KEY (user_id) REFERENCES myapp.users(id)
);
