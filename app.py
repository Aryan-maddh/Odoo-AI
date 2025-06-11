import os
import xmlrpc.client
import requests
import json
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from datetime import datetime, timedelta
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__, static_folder='.', static_url_path='')

# Odoo config (use environment variables for security)
ODOO_URL = os.getenv('ODOO_URL', 'http://209.182.234.38:8044')
ODOO_DB = os.getenv('ODOO_DB', 'v17-assets_management-demo')
ODOO_USERNAME = os.getenv('ODOO_USERNAME', 'admin')
ODOO_PASSWORD = os.getenv('ODOO_PASSWORD', 'admin@123')

# OdooReporter class
class OdooReporter:
    def __init__(self, url, db, username, password):
        self.url = url
        self.db = db
        self.username = username
        self.password = password
        
        self.common = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common')
        logging.debug(f"Attempting to authenticate with Odoo at {url}")
        self.uid = self.common.authenticate(db, username, password, {})
        if not self.uid:
            logging.error("Odoo authentication failed")
        else:
            logging.debug(f"Authenticated with Odoo, UID: {self.uid}")
        self.models = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object')

    def _execute(self, model, method, *args):
        try:
            logging.debug(f"Executing {model}.{method} with args: {args}")
            result = self.models.execute_kw(
                self.db, self.uid, self.password,
                model, method, list(args)
            )
            logging.debug(f"Result for {model}.{method}: {result}")
            return result
        except Exception as e:
            logging.error(f"Error on {model}.{method}: {e}")
            return []

    def get_today_new_leads(self):
        today = datetime.now().strftime('%Y-%m-%d')
        domain = [('create_date', '>=', today)]
        return self._execute('crm.lead', 'search_read', domain, ['name', 'create_date'])

    def get_today_won_leads(self):
        today = datetime.now().strftime('%Y-%m-%d')
        domain = [('date_closed', '>=', today), ('stage_id.is_won', '=', True)]
        return self._execute('crm.lead', 'search_read', domain, ['name', 'date_closed'])

    def get_today_lost_leads(self):
        today = datetime.now().strftime('%Y-%m-%d')
        domain = [('date_closed', '>=', today), ('stage_id.is_won', '=', False), ('active', '=', False)]
        return self._execute('crm.lead', 'search_read', domain, ['name', 'date_closed'])

    def get_running_delayed_leads(self):
        today = datetime.now().strftime('%Y-%m-%d')
        domain = [
            ('date_deadline', '<', today),
            ('stage_id.is_won', '=', False),
            ('active', '=', True),
            '|', ('user_id', '!=', False), ('team_id', '!=', False)
        ]
        return self._execute('crm.lead', 'search_read', domain, ['name', 'date_deadline', 'user_id', 'team_id'])

    def get_team_attendance(self):
        today = datetime.now().strftime('%Y-%m-%d')
        domain = [('active', '=', True)]
        employees = self._execute('hr.employee', 'search_read', domain, ['name', 'attendance_state'])
        present_count = sum(1 for e in employees if e.get('attendance_state') == 'checked_in')
        absent_members = [e['name'] for e in employees if e.get('attendance_state') != 'checked_in']
        return present_count, len(absent_members), absent_members

    def get_delayed_projects(self):
        today = datetime.now().strftime('%Y-%m-%d')
        domain = [
            ('date_end', '<', today),
            ('active', '=', True),
            ('stage_id.isClosed', '=', False)
        ]
        fields = ['name', 'date_end', 'stage_id']
        
        field_exists = self._execute('ir.model.fields', 'search_count', 
                                    [('model', '=', 'project.project'), ('name', '=', 'date_end')])
        if field_exists == 0:
            domain[0] = ('date', '<', today)
            fields[1] = 'date'
        
        projects = self._execute('project.project', 'search_read', domain, fields)
        return projects

    def get_incomplete_delayed_tasks(self):
        today = datetime.now().strftime('%Y-%m-%d')
        domain = [
            ('date_deadline', '<', today),
            ('active', '=', True),
            ('stage_id.is_closed', '=', False)
        ]
        fields = ['name', 'date_deadline', 'project_id', 'uid']
        
        field_exists = self._execute('ir.model.fields', 'search_count', 
                                    [('model', '=', 'project.task'), ('name', '=', 'date_deadline')])
        if field_exists == 0:
            domain[0] = ('date_end', '<', today)
            fields[1] = 'date_end'
            field_exists = self._execute('ir.model.fields', 'search_count', 
                                        [('model', '=', 'project.task'), ('name', '=', 'date_end')])
            if field_exists == 0:
                domain[0] = ('date', '<', today)
                fields[1] = 'date'
        
        tasks = self._execute('project.task', 'search_read', domain, fields)
        
        for task in tasks:
            if fields[1] != 'date_deadline':
                task['date_deadline'] = task.get('date', 'Unknown')
        
        return tasks

    def get_overdue_invoices(self):
        today = datetime.now().strftime('%Y-%m-%d')
        domain = [
            ('move_type', '=', 'out_invoice'),
            ('invoice_date_due', '<', today),
            ('payment_state', 'not in', ['paid', 'in_payment']),
            ('state', '=', 'posted')
        ]
        return self._execute('account.move', 'search_read', domain,
                           ['name', 'amount_total', 'invoice_date_due', 'payment_state', 'partner_id'])

    def get_delayed_quotations(self):
        today = datetime.now().strftime('%Y-%m-%d')
        one_week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        domain = [
            ('state', 'in', ['draft', 'sent']),
            ('date_order', '<', one_week_ago)
        ]
        return self._execute('sale.order', 'search_read', domain, ['name', 'date_order', 'partner_id'])

    def get_missing_timesheets(self):
        today = datetime.now()
        if today.weekday() >= 5:
            days_to_friday = today.weekday() - 4
            check_date = (today - timedelta(days=days_to_friday)).strftime('%Y-%m-%d')
        else:
            check_date = today.strftime('%Y-%m-%d')
        
        logging.info(f"Checking missing timesheets for date: {check_date}")
        
        try:
            employees = self._execute('hr.employee', 'search_read', 
                                    [('active', '=', True)], 
                                    ['name', 'id'])
            
            missing_employees = []
            
            for employee in employees:
                try:
                    timesheet_domain = [
                        ('employee_id', '=', employee['id']),
                        ('date', '=', check_date)
                    ]
                    timesheet_model = 'account.analytic.line'
                    model_check = self._execute('ir.model', 'search_count', 
                                              [('model', '=', timesheet_model)])
                    
                    if model_check > 0:
                        timesheets = self._execute(timesheet_model, 'search_count', timesheet_domain)
                        if timesheets == 0:
                            missing_employees.append(employee)
                    else:
                        logging.error(f"Timesheet model not found: {timesheet_model}")
                        
                except Exception as e:
                    logging.error(f"Error checking timesheets for {employee['name']}: {e}")
                    continue
            
            return missing_employees
            
        except Exception as e:
            logging.error(f"Error getting employees: {e}")
            return []

    def get_expected_revenue(self, scope='month'):
        today = datetime.now()
        if scope == 'month':
            start = today.replace(day=1).strftime('%Y-%m-%d')
            end = (today.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            end = end.strftime('%Y-%m-%d')
        else:
            start = (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')
            end = (today + timedelta(days=6 - today.weekday())).strftime('%Y-%m-%d')

        revenue_field = 'expected_revenue'
        field_exists = self._execute('ir.model.fields', 'search_count', 
                                    [('model', '=', 'crm.lead'), ('name', '=', 'expected_revenue')])
        if field_exists == 0:
            revenue_field = 'planned_revenue'
        
        domain = [
            (revenue_field, '!=', 0),
            ('create_date', '>=', start),
            ('create_date', '<=', end),
            ('active', '=', True),
            ('stage_id.is_won', '=', False)
        ]
        
        prob_exists = self._execute('ir.model.fields', 'search_count', 
                                  [('model', '=', 'crm.lead'), ('name', '=', 'probability')])
        if prob_exists:
            domain.append(('probability', '>', 0))
        
        leads = self._execute('crm.lead', 'search_read', domain, [revenue_field, 'probability'])
        
        total_revenue = 0
        for lead in leads:
            revenue = lead.get(revenue_field, 0)
            if prob_exists and lead.get('probability'):
                revenue *= lead['probability'] / 100
            total_revenue += revenue
        
        return total_revenue

    def get_hot_leads_this_week(self):
        today = datetime.now()
        start = (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')
        end = (today + timedelta(days=6 - today.weekday())).strftime('%Y-%m-%d')
        
        domain = [
            ('create_date', '>=', start),
            ('create_date', '<=', end),
            ('priority', '=', '3'),
            ('active', '=', True)
        ]
        fields = ['name', 'partner_id', 'create_date', 'expected_revenue']
        
        priority_exists = self._execute('ir.model.fields', 'search_count', 
                                       [('model', '=', 'crm.lead'), ('name', '=', 'priority')])
        if not priority_exists:
            domain = [
                ('create_date', '>=', start),
                ('create_date', '<=', end),
                ('expected_revenue', '>', 1000),
                ('active', '=', True)
            ]
        
        revenue_field = 'expected_revenue'
        field_exists = self._execute('ir.model.fields', 'search_count', 
                                    [('model', '=', 'crm.lead'), ('name', '=', 'expected_revenue')])
        if field_exists == 0:
            revenue_field = 'planned_revenue'
            fields[3] = 'planned_revenue'
        
        leads = self._execute('crm.lead', 'search_read', domain, fields)
        return leads

# Initialize OdooReporter
reporter = OdooReporter(ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD)

# Command mappings
COMMAND_MAPPINGS = {
    "today's new lead": ("get_today_new_leads", ['name', 'create_date']),
    "today's won leads": ("get_today_won_leads", ['name', 'date_closed']),
    "today's lost leads": ("get_today_lost_leads", ['name', 'date_closed']),
    "running delayed leads": ("get_running_delayed_leads", ['name', 'date_deadline', 'user_id', 'team_id']),
    "present team count": ("get_team_attendance", ['present_count', 'absent_count', 'absent_members']),
    "absent team info": ("get_team_attendance", ['present_count', 'absent_count', 'absent_members']),
    "delayed projects": ("get_delayed_projects", ['name', 'date_end', 'stage_id']),
    "overdue invoices": ("get_overdue_invoices", ['name', 'amount_total', 'invoice_date_due', 'partner_id']),
    "delayed quotation": ("get_delayed_quotations", ['name', 'date_order', 'partner_id']),
    "missing timesheet": ("get_missing_timesheets", ['name']),
    "monthly expected revenue": ("get_expected_revenue", ['total_revenue'], {'scope': 'month'}),
    "weekly expected revenue": ("get_expected_revenue", ['total_revenue'], {'scope': 'week'}),
    "hot leads this week": ("get_hot_leads_this_week", ['name', 'partner_id', 'create_date', 'expected_revenue']),
    "incomplete delayed tasks": ("get_incomplete_delayed_tasks", ['name', 'date_deadline', 'project_id', 'user_id'])
}

# Reusable HTML table generation
def generate_html_table(data, fields, empty_message="No records found"):
    if not data:
        return f"<p>{empty_message}</p>"
    html = "<table><tr>"
    for field in fields:
        html += f"<th>{field.title().replace('_', ' ')}</th>"
    html += "</tr>"
    for rec in data:
        html += "<tr>"
        for field in fields:
            value = rec.get(field, 'N/A')
            if field in ['user_id', 'team_id', 'partner_id', 'project_id', 'stage_id'] and isinstance(value, list):
                value = value[1] if len(value) > 1 else 'Unknown'
            elif field in ['amount_total', 'expected_revenue'] and isinstance(value, (int, float)):
                value = f"${value:.2f}"
            elif field in ['date_end', 'date_deadline'] and value == 'Unknown':
                value = rec.get('date', 'Unknown')
            html += f"<td>{value}</td>"
        html += "</tr>"
    html += "</table>"
    return html

# AI response generator with streaming
def generate(prompt):
    url = 'http://localhost:11434/api/generate'
    headers = {'Content-Type': 'application/json'}
    data = {
        "model": "llama3.2:3b",
        "prompt": prompt,
        "stream": True,
        "max_tokens": 100,
        "temperature": 0.7,
        "top_p": 0.9,
        "top_k": 50
    }
    try:
        response = requests.post(url, headers=headers, json=data, stream=True, timeout=60)
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if line:
                try:
                    json_obj = json.loads(line)
                    chunk = json_obj.get("response", "")
                    if chunk:
                        yield json.dumps({"type": "ai", "data": chunk})
                    if json_obj.get("done", False):
                        yield json.dumps({"type": "ai", "done": True})
                        break
                except json.JSONDecodeError:
                    logging.error(f"Invalid JSON line: {line}")
        yield json.dumps({"type": "ai", "done": True})
    except requests.Timeout:
        logging.error("Ollama request timed out")
        yield json.dumps({"type": "error", "data": "AI request timed out"})
    except requests.ConnectionError:
        logging.error("Failed to connect to Ollama server")
        yield json.dumps({"type": "error", "data": "Unable to connect to AI server"})
    except Exception as e:
        logging.error(f"Ollama error: {str(e)}")
        yield json.dumps({"type": "error", "data": f"AI response error: {str(e)}"})

@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@app.route('/service-worker.js')
def sw():
    return send_from_directory('static', 'sw.js')

@app.route('/process', methods=['POST'])
def process_input():
    logging.debug(f"Received request: {request.json}")
    user_message = request.json.get('message', '').strip().lower()
    if user_message.startswith('odoo '):
        user_message = user_message[5:].strip()
    logging.debug(f"Processing message: {user_message}")

    if user_message in COMMAND_MAPPINGS:
        logging.debug(f"Matched command: {user_message}")
        method_name, fields, kwargs = COMMAND_MAPPINGS[user_message] if len(COMMAND_MAPPINGS[user_message]) == 3 else (COMMAND_MAPPINGS[user_message][0], COMMAND_MAPPINGS[user_message][1], {})
        try:
            result = getattr(reporter, method_name)(**kwargs)
            logging.debug(f"Result for {method_name}: {result}")

            if method_name == 'get_team_attendance':
                present_count, absent_count, absent_members = result
                data = [{'present_count': present_count, 'absent_count': absent_count, 'absent_members': ', '.join(absent_members) or 'None'}]
                html = generate_html_table(data, ['present_count', 'absent_count', 'absent_members'])
                return jsonify({"type": "odoo", "data": html})

            if method_name == 'get_expected_revenue':
                data = [{'total_revenue': f"${result:.2f}"}]
                html = generate_html_table(data, ['total_revenue'])
                return jsonify({"type": "odoo", "data": html})

            if method_name == 'get_missing_timesheets':
                html = generate_html_table(result, ['name'], "All employees have submitted timesheets")
                return jsonify({"type": "odoo", "data": html})

            html = generate_html_table(result, fields)
            return jsonify({"type": "odoo", "data": html})

        except Exception as e:
            logging.error(f"Odoo error: {str(e)}")
            html = f"<p>Odoo error: {str(e)}</p>"
            return jsonify({"type": "odoo", "data": html})

    # Stream AI response
    def stream():
        for chunk in generate(user_message):
            yield chunk + '\n'
    return Response(stream_with_context(stream()), mimetype='application/x-ndjson')

if __name__ == '__main__':
    app.run(debug=True, ssl_context='adhoc')