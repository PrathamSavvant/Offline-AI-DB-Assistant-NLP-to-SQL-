CREATE TABLE organizations (
  organization_id INT PRIMARY KEY,
  code VARCHAR(12) NOT NULL UNIQUE,
  name VARCHAR(100) NOT NULL,
  status VARCHAR(20) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE locations (
  location_id INT PRIMARY KEY,
  code VARCHAR(12) NOT NULL UNIQUE,
  name VARCHAR(100) NOT NULL,
  status VARCHAR(20) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE commands (
  command_id INT PRIMARY KEY,
  organization_id INT NOT NULL,
  code VARCHAR(12) NOT NULL UNIQUE,
  name VARCHAR(100) NOT NULL,
  status VARCHAR(20) NOT NULL,
  CONSTRAINT fk_commands_organization FOREIGN KEY (organization_id)
    REFERENCES organizations(organization_id)
) ENGINE=InnoDB;

CREATE TABLE departments (
  department_id INT PRIMARY KEY,
  command_id INT NOT NULL,
  location_id INT NOT NULL,
  code VARCHAR(12) NOT NULL UNIQUE,
  name VARCHAR(100) NOT NULL,
  status VARCHAR(20) NOT NULL,
  CONSTRAINT fk_departments_command FOREIGN KEY (command_id) REFERENCES commands(command_id),
  CONSTRAINT fk_departments_location FOREIGN KEY (location_id) REFERENCES locations(location_id)
) ENGINE=InnoDB;

CREATE TABLE offices (
  office_id INT PRIMARY KEY,
  location_id INT NOT NULL,
  code VARCHAR(12) NOT NULL UNIQUE,
  name VARCHAR(100) NOT NULL,
  status VARCHAR(20) NOT NULL,
  CONSTRAINT fk_offices_location FOREIGN KEY (location_id) REFERENCES locations(location_id)
) ENGINE=InnoDB;

CREATE TABLE office_sections (
  office_id INT NOT NULL,
  section_code VARCHAR(12) NOT NULL,
  name VARCHAR(100) NOT NULL,
  status VARCHAR(20) NOT NULL,
  PRIMARY KEY (office_id, section_code),
  CONSTRAINT fk_office_sections_office FOREIGN KEY (office_id) REFERENCES offices(office_id)
) ENGINE=InnoDB;

CREATE TABLE employees (
  employee_id INT PRIMARY KEY,
  department_id INT NOT NULL,
  manager_id INT NULL,
  home_location_id INT NOT NULL,
  work_location_id INT NOT NULL,
  first_name VARCHAR(50) NOT NULL,
  last_name VARCHAR(50) NOT NULL,
  email VARCHAR(120) NOT NULL UNIQUE,
  hire_date DATE NOT NULL,
  status VARCHAR(20) NOT NULL,
  CONSTRAINT fk_employees_department FOREIGN KEY (department_id) REFERENCES departments(department_id),
  CONSTRAINT fk_employees_manager FOREIGN KEY (manager_id) REFERENCES employees(employee_id),
  CONSTRAINT fk_employees_home_location FOREIGN KEY (home_location_id) REFERENCES locations(location_id),
  CONSTRAINT fk_employees_work_location FOREIGN KEY (work_location_id) REFERENCES locations(location_id)
) ENGINE=InnoDB;

CREATE TABLE managers (
  manager_id INT PRIMARY KEY,
  employee_id INT NOT NULL UNIQUE,
  department_id INT NOT NULL,
  level_code VARCHAR(12) NOT NULL,
  status VARCHAR(20) NOT NULL,
  CONSTRAINT fk_managers_employee FOREIGN KEY (employee_id) REFERENCES employees(employee_id),
  CONSTRAINT fk_managers_department FOREIGN KEY (department_id) REFERENCES departments(department_id)
) ENGINE=InnoDB;

CREATE TABLE roles (
  role_id INT PRIMARY KEY,
  code VARCHAR(12) NOT NULL UNIQUE,
  name VARCHAR(100) NOT NULL,
  status VARCHAR(20) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE employee_roles (
  employee_id INT NOT NULL,
  role_id INT NOT NULL,
  assigned_date DATE NOT NULL,
  status VARCHAR(20) NOT NULL,
  PRIMARY KEY (employee_id, role_id),
  CONSTRAINT fk_employee_roles_employee FOREIGN KEY (employee_id) REFERENCES employees(employee_id),
  CONSTRAINT fk_employee_roles_role FOREIGN KEY (role_id) REFERENCES roles(role_id)
) ENGINE=InnoDB;

CREATE TABLE projects (
  project_id INT PRIMARY KEY,
  code VARCHAR(12) NOT NULL UNIQUE,
  name VARCHAR(100) NOT NULL,
  status VARCHAR(20) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE employee_projects (
  employee_id INT NOT NULL,
  project_id INT NOT NULL,
  allocation_percent INT NOT NULL,
  status VARCHAR(20) NOT NULL,
  PRIMARY KEY (employee_id, project_id),
  CONSTRAINT fk_employee_projects_employee FOREIGN KEY (employee_id) REFERENCES employees(employee_id),
  CONSTRAINT fk_employee_projects_project FOREIGN KEY (project_id) REFERENCES projects(project_id)
) ENGINE=InnoDB;

CREATE TABLE teams (
  team_id INT PRIMARY KEY,
  department_id INT NOT NULL,
  command_id INT NOT NULL,
  code VARCHAR(12) NOT NULL UNIQUE,
  name VARCHAR(100) NOT NULL,
  status VARCHAR(20) NOT NULL,
  CONSTRAINT fk_teams_department FOREIGN KEY (department_id) REFERENCES departments(department_id),
  CONSTRAINT fk_teams_command FOREIGN KEY (command_id) REFERENCES commands(command_id)
) ENGINE=InnoDB;

CREATE TABLE team_members (
  team_id INT NOT NULL,
  employee_id INT NOT NULL,
  joined_date DATE NOT NULL,
  status VARCHAR(20) NOT NULL,
  PRIMARY KEY (team_id, employee_id),
  CONSTRAINT fk_team_members_team FOREIGN KEY (team_id) REFERENCES teams(team_id),
  CONSTRAINT fk_team_members_employee FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
) ENGINE=InnoDB;

CREATE TABLE assignments (
  assignment_id INT PRIMARY KEY,
  employee_id INT NOT NULL UNIQUE,
  command_id INT NOT NULL,
  team_id INT NOT NULL,
  start_date DATE NOT NULL,
  status VARCHAR(20) NOT NULL,
  CONSTRAINT fk_assignments_employee FOREIGN KEY (employee_id) REFERENCES employees(employee_id),
  CONSTRAINT fk_assignments_command FOREIGN KEY (command_id) REFERENCES commands(command_id),
  CONSTRAINT fk_assignments_team FOREIGN KEY (team_id) REFERENCES teams(team_id)
) ENGINE=InnoDB;

CREATE TABLE historical_assignments (
  historical_assignment_id INT PRIMARY KEY,
  employee_id INT NOT NULL,
  command_id INT NOT NULL,
  team_id INT NOT NULL,
  start_date DATE NOT NULL,
  end_date DATE NOT NULL,
  status VARCHAR(20) NOT NULL,
  CONSTRAINT fk_historical_assignments_employee FOREIGN KEY (employee_id) REFERENCES employees(employee_id),
  CONSTRAINT fk_historical_assignments_command FOREIGN KEY (command_id) REFERENCES commands(command_id),
  CONSTRAINT fk_historical_assignments_team FOREIGN KEY (team_id) REFERENCES teams(team_id)
) ENGINE=InnoDB;

CREATE TABLE employee_office_sections (
  employee_id INT NOT NULL,
  office_id INT NOT NULL,
  section_code VARCHAR(12) NOT NULL,
  assigned_date DATE NOT NULL,
  PRIMARY KEY (employee_id, office_id, section_code),
  CONSTRAINT fk_employee_office_sections_employee FOREIGN KEY (employee_id) REFERENCES employees(employee_id),
  CONSTRAINT fk_employee_office_sections_section FOREIGN KEY (office_id, section_code)
    REFERENCES office_sections(office_id, section_code)
) ENGINE=InnoDB;

CREATE TABLE salaries (
  employee_id INT PRIMARY KEY,
  annual_salary DECIMAL(12,2) NOT NULL,
  effective_date DATE NOT NULL,
  status VARCHAR(20) NOT NULL,
  CONSTRAINT fk_salaries_employee FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
) ENGINE=InnoDB;

CREATE TABLE certifications (
  certification_id INT PRIMARY KEY,
  employee_id INT NOT NULL,
  code VARCHAR(12) NOT NULL,
  name VARCHAR(100) NOT NULL,
  status VARCHAR(20) NOT NULL,
  CONSTRAINT fk_certifications_employee FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
) ENGINE=InnoDB;

CREATE TABLE audit_events (
  id INT PRIMARY KEY,
  location_id INT NOT NULL,
  code VARCHAR(12) NOT NULL,
  name VARCHAR(100) NOT NULL,
  status VARCHAR(20) NOT NULL,
  details VARCHAR(255) NOT NULL
) ENGINE=InnoDB;
