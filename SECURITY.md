# Security Policy

## Supported version

Security fixes are applied to the latest version on the default branch.

## Reporting a vulnerability

Please do not open a public issue containing credentials, database contents,
private schema details, logs, or an exploitable proof of concept.

Use GitHub's private vulnerability reporting feature when it is available for
this repository. Include:

- the affected component and version or commit;
- the expected and observed behavior;
- a minimal reproduction using synthetic data;
- the potential impact; and
- any mitigation already tested.

Do not include production database exports, `.env` files, model prompts that
contain private business data, or customer information.

## Deployment expectations

AI Database Assistant is designed around layered controls, not model trust:

- the MySQL account should be read-only and least privilege;
- the web server should remain bound to loopback unless authenticated network
  access is explicitly implemented;
- generated SQL must pass the SQLGlot-based validator;
- approved business relationships must be reviewed after endpoint or purpose changes;
- logs and generated knowledge must be handled as potentially sensitive data; and
- the offline package should be transferred and updated through an approved process.
