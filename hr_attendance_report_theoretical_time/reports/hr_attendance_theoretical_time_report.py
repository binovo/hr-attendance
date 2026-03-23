# Copyright 2017-2019 Tecnativa - Pedro M. Baeza
# Copyright 2021 Tecnativa - Víctor Martínez
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from datetime import datetime, time

import pytz
from psycopg2.extensions import AsIs

from odoo import api, fields, models, tools


class HrAttendanceTheoreticalTimeReport(models.Model):
    _name = "hr.attendance.theoretical.time.report"
    _description = "Report of theoretical time vs attendance time"
    _auto = False
    _rec_name = "date"
    _order = "date,employee_id,theoretical_hours desc"

    employee_id = fields.Many2one(
        comodel_name="hr.employee", string="Employee", readonly=True
    )
    company_id = fields.Many2one(related="employee_id.company_id")
    department_id = fields.Many2one(
        comodel_name="hr.department",
        string="Department",
        readonly=True,
    )
    date = fields.Date(readonly=True)
    worked_hours = fields.Float(string="Worked", readonly=True)
    theoretical_hours = fields.Float(string="Theoric", readonly=True)
    difference = fields.Float(readonly=True)

    def _select(self):
        # We put "max" aggregation function for theoretical hours because
        # we will recompute for other detail levels different than day
        # through recursivity by day results and will aggregate them manually
        return """
            min(id) AS id,
            employee_id,
            department_id,
            date,
            sum(worked_hours) AS worked_hours,
            max(theoretical_hours) AS theoretical_hours,
            sum(difference) AS difference
            """

    def _select_sub1(self):
        # Unique ID is assured (mostly, as we lose some precission) through
        # this MD5 hash converted to integer. See
        # https://stackoverflow.com/a/9812029 for details.
        return """
            (
                ('x'||substr(MD5('HA' || ha.id::text), 1, 8))::bit(32)::int
            ) AS id,
            ha.employee_id AS employee_id,
            hahe.department_id AS department_id,
            ha.check_in::date AS date,
            ha.worked_hours AS worked_hours,
            ha.theoretical_hours AS theoretical_hours,
            0.0 AS difference
            """

    def _from_sub1(self):
        return """
            hr_attendance ha
            LEFT JOIN hr_employee AS hahe ON ha.employee_id = hahe.id
            """

    def _where_sub1(self):
        return "True"

    def _select_sub2(self):
        # Same comment about ID uniqueness of sub1.
        return """
            (
                ('x'||substr(MD5(
                    'HE' || he.id::text || gs::text
                ), 1, 8))::bit(32)::int
            ) AS id,
            he.id AS employee_id,
            he.department_id AS department_id,
            gs::date AS date,
            0 AS worked_hours,
            -1 AS theoretical_hours,
            0.0 AS difference
            """

    def _from_sub2(self):
        # We generate one record for each of the theoretical working days
        # since the employee creation / working schedule beginning for not
        # depending on the registered attendances.
        return """
                hr_employee he
            INNER JOIN
                resource_resource rr ON he.resource_id = rr.id
            LEFT JOIN
                resource_calendar_attendance rca
                    ON rca.calendar_id = rr.calendar_id
            CROSS JOIN
                generate_series(
                    greatest(
                        COALESCE(he.theoretical_hours_start_date,
                                 he.create_date::date),
                        COALESCE(rca.date_from,
                                 he.theoretical_hours_start_date,
                                 he.create_date::date)
                    )
                    + (8 + rca.dayofweek::int -
                        extract(dow from greatest(
                            COALESCE(he.theoretical_hours_start_date,
                                     he.create_date::date),
                            COALESCE(rca.date_from,
                                     he.theoretical_hours_start_date,
                                     he.create_date::date)
                        ))::int) % 7,
                    least(
                        COALESCE(rca.date_to, current_date),
                        current_date
                    )
                    + (-6 + rca.dayofweek::int -
                        extract(dow from least(
                            COALESCE(rca.date_to, current_date),
                            current_date
                        ))::int) % 7,
                    '7 days'
                ) AS gs
            """

    def _where_sub2(self):
        return """
            rca.id IS NOT NULL
            """

    def _group_by(self):
        return """
            employee_id,
            department_id,
            date
            """

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(
            """
CREATE or REPLACE VIEW %s as (
    SELECT %s
    FROM (
        (
            SELECT %s
            FROM %s
            WHERE %s
        )
        UNION (
            SELECT %s
            FROM %s
            WHERE %s
        )
    ) AS u
    GROUP BY %s
)
            """,
            (
                AsIs(self._table),
                AsIs(self._select()),
                AsIs(self._select_sub1()),
                AsIs(self._from_sub1()),
                AsIs(self._where_sub1()),
                AsIs(self._select_sub2()),
                AsIs(self._from_sub2()),
                AsIs(self._where_sub2()),
                AsIs(self._group_by()),
            ),
        )
        self.env.cr.execute("""
    CREATE INDEX IF NOT EXISTS hr_employee_theoretical_hours_start_date_index
                            ON hr_employee (theoretical_hours_start_date, (create_date::date));
            """)
        self.env.cr.execute("""
    CREATE INDEX IF NOT EXISTS hr_attendance_emp_date_index
                            ON hr_attendance (employee_id, check_in);
            """)
        self.env.cr.execute("""
    CREATE INDEX IF NOT EXISTS resource_calendar_attendance_calendar_id_index
                            ON resource_calendar_attendance (calendar_id);
            """)
        self.env.cr.execute("""
    CREATE INDEX IF NOT EXISTS hr_employee_department_id_index
                            ON hr_employee (department_id);
            """)

    # TODO: To be activated for performance assuring cache clearing on changes
    # @tools.ormcache('employee.id', 'date')
    @api.model
    def _theoretical_hours(self, employee, date):
        """Get theoretical working hours for the day where the check-in is
        done for that employee.
        """
        if not employee.resource_id.calendar_id:
            return 0
        tz = employee.resource_id.calendar_id.tz
        res = employee.with_context(
            exclude_public_holidays=True, employee_id=employee.id
        )._get_work_days_data_batch(
            datetime.combine(date, time(0, 0, 0, 0, tzinfo=pytz.timezone(tz))),
            datetime.combine(date, time(23, 59, 59, 99999, tzinfo=pytz.timezone(tz))),
            # Pass this domain for excluding leaves whose type is included in
            # theoretical hours
            domain=[
                "|",
                ("holiday_id", "=", False),
                ("holiday_id.holiday_status_id.include_in_theoretical", "=", False),
            ],
        )
        return res[employee.id]["hours"]

    @api.model
    def read_group(self, domain, fields, groupby, offset=0, limit=None, orderby=False, lazy=True):
        # Dynamically computes theoretical hours in an optimized way to prevent N+1 query performance issues.
        # Instead of computing hours record-by-record for non-existing attendances (marked as < 0),
        # this method groups the pending calculations by Employee Timezone and Date.
        # It leverages Odoo's native batch processing (`_get_work_days_data_batch`) to compute
        # multiple employees in a single call per day/timezone combination, drastically improving
        # pivot view performance. Finally, it aggregates the totals and updates the differences.
        res = super(HrAttendanceTheoreticalTimeReport, self).read_group(
            domain, fields, groupby, offset=offset, limit=limit, orderby=orderby, lazy=lazy
        )
        if "theoretical_hours:sum" not in fields:
            return res
        full_fields = all(x in fields for x in {"theoretical_hours:sum", "worked_hours:sum", "difference:sum"})
        difference_field = "difference:sum" in fields
        HrEmployee = self.env['hr.employee']
        for line in res:
            line_domain = line.get("__domain", domain)
            records = self.search_read(line_domain, ['employee_id', 'date', 'theoretical_hours'])
            day_dict = {}
            needs_compute_keys = []
            unique_emp_ids = set()
            for data in records:
                emp_id = data['employee_id'][0] if data['employee_id'] else False
                date = data['date']
                if not emp_id or not date:
                    continue
                key = (emp_id, date)
                if key not in day_dict:
                    if data['theoretical_hours'] < 0:
                        needs_compute_keys.append(key)
                        unique_emp_ids.add(emp_id)
                        day_dict[key] = 0.0
                    else:
                        day_dict[key] = data['theoretical_hours']
            if unique_emp_ids:
                employees = HrEmployee.browse(list(unique_emp_ids))
                emp_to_tz = {}
                for emp in employees:
                    tz_name = emp.resource_calendar_id.tz or emp.tz or 'UTC'
                    emp_to_tz[emp.id] = tz_name
                compute_batches = {}
                for emp_id, date in needs_compute_keys:
                    tz_name = emp_to_tz[emp_id]
                    if tz_name not in compute_batches:
                        compute_batches[tz_name] = {}
                    if date not in compute_batches[tz_name]:
                        compute_batches[tz_name][date] = []
                    compute_batches[tz_name][date].append(emp_id)
                for tz_name, dates_dict in compute_batches.items():
                    tz = pytz.timezone(tz_name)
                    for date, emp_ids in dates_dict.items():
                        emps_for_date = HrEmployee.browse(emp_ids)
                        dt_from = datetime.combine(date, time(0, 0, 0)).replace(tzinfo=tz)
                        dt_to = datetime.combine(date, time(23, 59, 59)).replace(tzinfo=tz)
                        work_data = emps_for_date.with_context(
                            exclude_public_holidays=True
                        )._get_work_days_data_batch(
                            dt_from, dt_to,
                            domain=[
                                "|",
                                ("holiday_id", "=", False),
                                ("holiday_id.holiday_status_id.include_in_theoretical", "=", False),
                            ]
                        )
                        for e_id, w_data in work_data.items():
                            day_dict[(e_id, date)] = w_data['hours']
            line["theoretical_hours"] = sum(day_dict.values())
            if full_fields:
                line["difference"] = (line.get("worked_hours", 0.0) or 0.0) - line["theoretical_hours"]
            elif difference_field and "difference" in line:
                del line["difference"]
        return res
