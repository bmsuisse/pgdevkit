-- the body contains ';', so it needs a DELIMITER
DROP PROCEDURE IF EXISTS widget_count;
DELIMITER $$
CREATE PROCEDURE widget_count()
BEGIN
    DECLARE n INT;
    SELECT count(*) INTO n FROM widget;
    SELECT n AS n;
END$$
DELIMITER ;
