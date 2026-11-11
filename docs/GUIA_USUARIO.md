# Guía de usuario · Centro de reservas Action Sport Club

Esta guía explica cómo usar la aplicación en el día a día. No necesitas saber SQL: la app traduce cada consulta a lenguaje de negocio.

---

## 1. Primeros pasos

Al abrir la app verás un **menú a la izquierda** con dos controles:

| Control | Para qué sirve |
|---|---|
| **Área** | Filtra el menú según tu rol: *Gerencia comercial*, *Gerencia operativa* o *Todas las áreas*. |
| **Menú** | Las páginas de la app. Cambia según el área elegida. |

Abajo del menú están la **hora de Bogotá** y el botón **↻ Actualizar datos**. La app guarda las consultas por 5 minutos para responder rápido. Usa este botón si alguien más modificó datos y quieres verlos al instante.

> Toda acción que guardas (una reserva, un cambio de estado) actualiza los datos de inmediato.

---

## 2. Páginas para la operación diaria

### 2.1 Inicio (panel del club)
La vista general del día:
- **Indicadores:** canchas disponibles, canchas en mantenimiento o cerradas, usuarios activos, recaudo del mes, reservas de los próximos 7 días y solicitudes por confirmar.
- **Usuarios solicitando canchas:** reservas pendientes o reservadas, de la más próxima a la más lejana.
- **Canchas más solicitadas** en los últimos 30 días.
- **Estado de las canchas por sede.**

Los botones **Solicitar una cancha** y **Registrar usuario** llevan directo a esas páginas.

### 2.2 Solicitar cancha
1. Elige el **usuario** que solicita. Solo aparecen usuarios activos; puedes escribir para buscar por nombre o documento.
2. Elige **sede**, **deporte** y **cancha**. Solo aparecen canchas disponibles.
3. Elige **fecha** y **duración** (1, 2 o 3 horas).
4. En **Hora de inicio** solo aparecen horarios libres. La app descarta las horas que se cruzan con otra reserva y las que terminarían después de las 22:00.
5. Elige el **método de pago** y revisa el **valor total**.
6. Pulsa **Solicitar cancha**. La reserva queda en estado **Pendiente**.

> Si otra persona tomó ese horario mientras elegías, la app no guarda nada y te avisa.

En la parte superior, **Consultar solicitantes registrados** muestra la tabla de usuarios con su estado, número de reservas y reservas vencidas.

### 2.3 Registrar usuario
1. Completa documento, tipo de usuario, nombre, apellido, correo y teléfono (opcional).
2. Pulsa **Registrar usuario**.
3. Usa el botón **Solicitar una cancha para este usuario**: la app abre la solicitud con ese usuario ya seleccionado.

Reglas: el documento tiene de 5 a 20 letras o números; el correo debe ser válido; no se permiten documentos ni correos repetidos.

### 2.4 Reservas por estado
- **Contadores** de pendientes, reservadas, pagadas, vencidas y canceladas.
- **Aviso de vencidas:** si hay reservas sin pago cuya hora ya pasó, el botón **Marcar como vencidas** las cierra de una vez.
- **Pestañas por estado**, con filtro por sede y opción de ver solo reservas de hoy en adelante.
- **Cambiar el estado de una reserva:** elige el estado actual, la reserva y el nuevo estado. La app solo ofrece los cambios permitidos:

```
PENDIENTE  →  RESERVADA  →  PAGADA
    ↓             ↓            ↓
 VENCIDA /    VENCIDA /    CANCELADA
 CANCELADA    CANCELADA
```

- **Calificar una reserva pagada:** solo aparecen reservas pagadas y ya terminadas, de 1 a 5 estrellas.

### 2.5 Canchas
- Tabla con todas las canchas: sede, deporte, superficie, capacidad, precio, estado y reservas futuras.
- **Actualizar una cancha:** cambia el estado (Disponible, Mantenimiento, Cerrada) o el precio por hora.
- Si sacas de servicio una cancha con reservas futuras, la app las muestra y te deja **cancelarlas en el mismo paso**.

> Un cambio de precio solo aplica a reservas nuevas. Las existentes conservan su valor.

---

## 3. Páginas para la gerencia

### 3.1 Consultas guiadas
La función central para la gerencia: preguntas de negocio que se responden con SQL, sin escribir código.

1. En **¿Qué quieres saber?** elige una pregunta. Cada pregunta indica su área: *Comercial* u *Operativa*.
2. Ajusta los **filtros** que aparezcan: periodo, sede, deporte, cantidad a mostrar o día.
3. Lee la **gráfica** y la **tabla**.
4. Usa **⬇ Descargar Excel** para llevarte el resultado.
5. Abre **Ver la consulta SQL** si quieres ver exactamente qué se ejecutó.

| Gerencia comercial | Gerencia operativa |
|---|---|
| ¿Cuánto recaudamos cada mes en cada sede? | ¿Qué tan ocupada está cada cancha? |
| ¿Qué canchas generan más ingresos? | ¿Cuáles son las horas pico de la semana? |
| ¿Cuál es el ticket promedio por deporte? | ¿Qué reservas hay para un día? |
| ¿Quiénes son los clientes que más gastan? | ¿Qué deportes tienen más demanda? |
| ¿Qué medios de pago prefieren los clientes? | ¿Se juega más en festivos y fines de semana? |
| ¿Cuánto dinero está pendiente de cobro? | ¿Qué canchas tienen peor calificación? |
| ¿Cuánto perdimos por vencidas o canceladas? | ¿Qué usuarios acumulan reservas vencidas? |
| ¿Qué clientes llevan tiempo sin reservar? | ¿Cómo se distribuyen las reservas por estado? |
| ¿Cómo se comporta cada tipo de usuario? | |
| ¿Cómo evolucionan las reservas semana a semana? | |

### 3.2 Análisis
Un tablero con filtros de **periodo** y **sede**:
- **Indicadores:** reservas, usuarios, % pagadas, % vencidas, ticket promedio y calificación.
- **Demanda:** horas por deporte, estados y ocupación por cancha.
- **Horarios:** mapa de calor de horas pico, tipo de día y tendencia semanal.
- **Ingresos:** recaudo por mes y sede, medios de pago y dinero no cobrado.
- **Clientes y calidad:** gasto por tipo de usuario y calificación por cancha.

Pasa el cursor sobre cualquier barra para ver el detalle.

### 3.3 Segmentación
Agrupa a los clientes para dirigir promociones.

**Pestaña Modelo K-means**
1. Oracle calcula, por usuario: días sin jugar, reservas pagadas, gasto, % de vencidas y % de canceladas.
2. La app sugiere el número de grupos (**k**) con el coeficiente de silueta. Puedes cambiarlo.
3. Revisa el **perfil de cada segmento** y su **promoción sugerida**:

| Segmento | Promoción sugerida |
|---|---|
| VIP frecuentes | Prioridad en horas pico y una hora gratis cada 10 reservas |
| Ocasionales | Segunda hora con 20% de descuento |
| Nuevos / bajo consumo | Paquete de bienvenida en horario valle |
| Dormidos | 25% en la próxima reserva si juegan este mes |
| En mora | Sin promociones; pago anticipado hasta regularizar |

4. El **mapa de segmentos** ubica a cada cliente por días sin jugar y gasto.
5. Elige un segmento para ver sus usuarios con correo y teléfono, y descarga el Excel para la campaña.

**Pestaña Reglas con SQL**
Segmentos con reglas fijas (`CASE`) y umbrales que ajustas con los controles: frecuente, dormido, en mora, ocasional y sin consumo.

### 3.4 Consultas SQL (modo experto)
Para quien sabe SQL. Acepta solo consultas de lectura (`SELECT` o `WITH`), sin punto y coma, con un máximo de 2.000 filas. El resultado también se descarga a Excel.

---

## 4. Preguntas frecuentes

**¿Por qué no aparece un usuario al solicitar una cancha?**
Porque está inactivo o bloqueado. Solo los usuarios activos pueden reservar.

**¿Por qué no aparece una hora que se ve libre?**
Porque, con la duración elegida, se cruzaría con otra reserva o terminaría después de las 22:00. Prueba con menos horas.

**¿Puedo borrar una reserva?**
No. Por seguridad, la app no borra datos. Una reserva se cancela, y queda en el historial para los análisis.

**¿Por qué no puedo calificar una reserva?**
Solo se califican reservas pagadas cuya hora de juego ya terminó.

**Los datos no se actualizan.**
Pulsa **↻ Actualizar datos** en el menú lateral.
