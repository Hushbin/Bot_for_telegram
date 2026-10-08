# OCI Terraform repository and authentication plan

Status: proposed, 8 October 2026. This is a plan for a new infrastructure repository; no OCI identities, keys, policies, or cloud resources have been created.

## Decision: repository layout

Start a separate `smebot-oci-infra` repository. This bot repository currently tracks its entire Python 3.8 virtual environment and bytecode (over 3,200 files), has no `.gitignore` or dependency manifest, and is still an early application prototype. A clean infrastructure repository gives Terraform its own review rules, state handling, and deployment history while the application is cleaned up. This is a workflow choice, **not** a credential boundary: repository separation does not protect a committed key or state file.

If the same people eventually change the bot and its infrastructure in one release, an `infra/` directory in a cleaned-up application repository is also reasonable. Nothing in OCI or Terraform requires separate repositories. Put a link to the infrastructure repository in the bot README once it exists; pass only needed resource identifiers to the application deployment, not Terraform state or credentials.

Suggested new repository skeleton:

```text
smebot-oci-infra/
  README.md
  .gitignore
  bootstrap/README.md       # manual tenancy/IAM/state setup
  envs/dev/                # root Terraform module and backend config
    .terraform.lock.hcl    # generated from this root module
  modules/                 # only after duplication justifies modules
```

Commit `.terraform.lock.hcl` from each root module. Ignore `.terraform/`, `*.tfstate*`, `*.tfplan`, `crash.log`, `*.tfvars`, `*.pem`, `.env*`, and any downloaded database wallet. Keep example variable files with placeholders only. Keep OCI API keys and database passwords out of Git history, CI logs, plan artifacts, and Terraform outputs.

## What the current bot actually needs

- `handlers.py` creates a `User` record in Fauna and updates `is_smeowner` by record ID. There is no Fauna read or delete path yet.
- Cloudinary is configured and `upload` is imported, but no handler calls it. Object Storage is a future feature rather than an existing upload migration.
- `main.py` polls Telegram; conversation state exists only in process memory, and only two of the declared states have handlers. OCI infrastructure alone will not make this bot production ready.
- `Updating_plan.md` is a draft, not an implementation specification. Verify its proposed database service and Always Free eligibility against the current OCI console before writing Terraform. Its AWS-style bucket policy example is not OCI IAM syntax, and OCI Vision is not a drop-in replacement for Cloudinary transformations. Avoid provisioning either for the first authentication test.

## Authentication model

Use **OCI API signing keys** for local Terraform. Use a separate automation identity and API key only if a hosted CI runner is added. An OCI-hosted runner could later use an **instance principal** and avoid a long-lived CI key, but that is a separate deployment design. Do not assume a Git-hosted runner can use OCI instance/resource principal authentication or direct OIDC federation without first verifying a supported flow.

Terraform provider authentication and Terraform **state backend authentication are separate**. `terraform login` authenticates to HCP Terraform, not to OCI.

| Context | Identity | Credential location | Initial scope |
| --- | --- | --- | --- |
| Developer workstation | Personal OCI IAM user in an infra group | API private key and `~/.oci/config` outside Git | Bot compartment resources |
| Hosted CI, if added | Dedicated automation user in the same or narrower group | CI secret store; write a temporary key file during the job | Bot compartment resources |
| OCI-hosted runner, optional later | OCI instance principal plus dynamic group | No API private key on the runner | Bot compartment resources |

### 1. Bootstrap OCI access outside Terraform

An existing tenancy administrator should:

1. Confirm the tenancy, chosen region, identity domain, and target compartment. Create a dedicated bot compartment if needed. Record tenancy OCID, compartment OCID, region, and Object Storage namespace as nonsecret setup values.
2. Create an IAM group for Terraform operators. Add the developer's user. If hosted CI is planned, create a separate automation user and add it to a group with only the permissions its jobs need. Use MFA on human accounts; restrict and monitor the automation user.
3. Create IAM policies scoped to the bot compartment for resources Terraform will *actually* manage. For a first Object Storage bucket, start with bucket permissions; add object permissions only if Terraform manages objects. Add database permissions only after the database choice is validated. Grant compartment inspection at tenancy scope if provider lookups require it. Account for identity-domain-qualified group names in the policy syntax. Do not grant `manage all-resources in tenancy` for convenience.
4. Keep creation of the initial compartment, IAM group/policies, and state location as an explicit bootstrap step. Terraform cannot create its own first credentials or grant itself its first permissions. If IAM is later managed as code, use a separate, tightly reviewed administrative stack.

Example policy *shapes* to adapt and validate in the OCI policy editor:

```text
Allow group <domain>/<group> to inspect compartments in tenancy
Allow group <domain>/<group> to manage buckets in compartment <bot-compartment>
```

Add the appropriate database and network policy statements only when their resource plan is known. Resource creation may need more than these two example statements.

### 2. Set up the local API signing key

1. Generate a dedicated RSA API signing key pair on the workstation, or use the OCI CLI's configuration setup. For example:

   ```sh
   mkdir -p ~/.oci
   chmod 700 ~/.oci
   openssl genrsa -out ~/.oci/smebot_api_key.pem 2048
   chmod 600 ~/.oci/smebot_api_key.pem
   openssl rsa -in ~/.oci/smebot_api_key.pem -pubout -out ~/.oci/smebot_api_key_public.pem
   ```

   Upload **only the public key** in the OCI Console under the personal user's API Keys. Record the displayed fingerprint and the user OCID. Keep the private key outside Git.
2. Create `~/.oci/config` with a named profile. Keep the file outside both repositories and limit its permissions. Use an absolute path for `key_file`:

   ```ini
   [SMEBOT_TF]
   user=ocid1.user.oc1...placeholder
   fingerprint=placeholder
   tenancy=ocid1.tenancy.oc1...placeholder
   region=your-oci-region
   key_file=/absolute/path/to/.oci/smebot_api_key.pem
   ```

3. In the new repo, declare the `oracle/oci` provider, pin a tested provider version, and commit the generated lock file. For local use, point the provider at the named profile:

   ```hcl
   provider "oci" {
     config_file_profile = "SMEBOT_TF"
   }
   ```

   Supply compartment/tenancy OCIDs as ordinary configuration inputs where needed. Do not set the private key value in HCL or a `.tfvars` file.
4. Run `terraform init`, `terraform fmt -check`, and `terraform validate`. Then run a plan that includes a read-only OCI data lookup in the target compartment; syntax checks alone do **not** prove authentication. Confirm the lookup succeeds and review the IAM policy scope. Stop at `plan` until the resource and state design is reviewed.

### 3. Choose state storage before the first shared apply

Terraform state and saved plans can contain passwords and other sensitive values even when HCL uses `sensitive = true`. Never commit them. Decide who can read state, how it is encrypted, versioned/backed up, and how concurrent applies are prevented.

Recommended first shared workflow: evaluate **OCI Resource Manager** for managed Terraform jobs/state and verify its current source-control integration and provider authentication in the chosen tenancy. If running Terraform from local machines or hosted CI instead, choose and test a remote backend before applying real infrastructure. OCI Object Storage through an S3-compatible backend can require a separate **Customer Secret Key** for backend access; the provider's API signing key does not authenticate that backend. Confirm locking and recovery behavior for the exact backend/version selected; an Object Storage bucket by itself is not proof of safe state locking. One private local state file is acceptable only for a disposable, single-operator experiment, with no database passwords and no shared applies.

Document the selected backend, bootstrap procedure, access policy, restore test, and lock strategy in the new repo before enabling CI apply.

### 4. Add hosted CI only after local auth and state work

1. Create a separate automation user and API signing key. Store the **private key** as a masked CI secret; store tenancy OCID, user OCID, fingerprint, region, and compartment OCID as CI variables or secrets according to repository policy. Never place the key in the repo or workflow YAML.
2. In a trusted job, write the private key to a temporary file with mode `0600`, assemble a temporary OCI config profile, and point the provider at that profile. Mask secrets and remove temporary files after the job. Keep the key out of command tracing and uploaded artifacts.
3. Run unauthenticated format/validate checks on pull requests. Run an authenticated plan only on trusted code with access to the protected CI environment. Apply only from the protected main branch after human approval. Do not run unreviewed pull-request code with OCI secrets; in GitHub Actions, avoid `pull_request_target` plus checkout of untrusted PR code.
4. Serialize applies for each state and verify the backend's own locking. Give CI no tenancy-wide IAM administration. Rotate/revoke the CI API key independently of the developer's key, and test that a revoked key can no longer access OCI.

## Completion criteria for the first milestone

- [ ] Dedicated infra repository exists with ignore rules, provider version constraint, lock file, and a documented bootstrap boundary.
- [ ] Terraform operator can perform a read-only OCI lookup in the target compartment using a named local profile.
- [ ] IAM policy grants only the chosen compartment permissions; a permission review confirms no tenancy-wide resource administration was added.
- [ ] State backend choice, authentication, locking, backup/restore, and access control are documented before real `apply`.
- [ ] First resource plan contains only validated Always Free eligible resources; estimated cost and budget alerts are reviewed (alerts do not cap spending).
- [ ] If CI is enabled, its identity, protected environment, secret handling, plan/apply gates, and revocation procedure are tested.

## Decisions to make when creating the new repo

1. Git host and whether CI is needed immediately. Local Terraform with a personal OCI profile is enough for the first read-only test.
2. OCI region and compartment name.
3. Whether the initial infrastructure is only Object Storage, or also a database after checking the current Always Free options. The existing code does not yet need Object Storage to run.
4. State execution model: OCI Resource Manager or a tested remote backend for local/CI Terraform.
