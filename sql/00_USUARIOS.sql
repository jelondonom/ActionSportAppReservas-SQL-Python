-- =====================================================================================
-- 00_USUARIOS.sql  |  Ejecutar UNA sola vez, conectado como ADMIN.
-- Crea dos usuarios para no trabajar con ADMIN:
--   RESERVAS     : dueño de las tablas. Con él se ejecutan 01_DDL.sql y 02_DML.sql.
--   APP_RESERVAS : usuario de la aplicación. Solo se conecta; los permisos sobre las
--                  tablas (sin DELETE) se otorgan al final de 01_DDL.sql.
-- Contraseñas: 12 a 30 caracteres, con mayúscula, minúscula y número; sin el nombre
-- del usuario. Reemplace los textos entre comillas antes de ejecutar.
-- =====================================================================================

CREATE USER reservas IDENTIFIED BY "Cambie_Esta_Clave_1";
GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE TO reservas;
ALTER USER reservas QUOTA UNLIMITED ON DATA;

CREATE USER app_reservas IDENTIFIED BY "Cambie_Esta_Clave_2";
GRANT CREATE SESSION TO app_reservas;
