FLUSH PRIVILEGES;

CREATE DATABASE IF NOT EXISTS enterprise_catalog;
GRANT ALL ON enterprise_catalog.* TO 'entcatalog001'@'%' IDENTIFIED BY 'password';

GRANT ALL ON test_enterprise_catalog.* TO 'entcatalog001'@'%' IDENTIFIED BY 'password';

FLUSH PRIVILEGES;
