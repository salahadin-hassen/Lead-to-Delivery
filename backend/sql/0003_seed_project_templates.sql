-- 0003: seed the three initial project templates.
-- Definitions follow app.schemas.templates.TemplateDefinition (schema_version 1);
-- tests/test_templates.py validates every seeded row against it.
-- ON CONFLICT keeps the seed harmless if a template with the same key was
-- created by hand before this migration ran.

INSERT INTO project_templates (key, name, description, version, definition) VALUES
(
    'website_build',
    'Website Build',
    'Discovery, design, build and launch of a marketing website.',
    1,
    $json${
      "schema_version": 1,
      "default_duration_days": 42,
      "milestones": [
        {"key": "discovery", "name": "Discovery", "due_offset_days": 7, "tasks": [
          {"key": "kickoff_call", "title": "Kickoff call", "due_offset_days": 2, "client_visible": true, "assignee_role": "account_manager",
           "description": "Confirm goals, audience, scope and timeline with the client."},
          {"key": "collect_brand_assets", "title": "Collect brand assets and content", "due_offset_days": 5, "client_visible": true, "assignee_role": "account_manager"},
          {"key": "sitemap", "title": "Draft sitemap and page list", "due_offset_days": 7, "client_visible": true, "assignee_role": "designer"}
        ]},
        {"key": "design", "name": "Design", "due_offset_days": 18, "tasks": [
          {"key": "wireframes", "title": "Wireframes for key pages", "due_offset_days": 12, "client_visible": true, "assignee_role": "designer"},
          {"key": "visual_design", "title": "Visual design and client approval", "due_offset_days": 18, "client_visible": true, "assignee_role": "designer"}
        ]},
        {"key": "build", "name": "Build", "due_offset_days": 35, "tasks": [
          {"key": "dev_environment", "title": "Set up hosting and staging environment", "due_offset_days": 20, "client_visible": false, "assignee_role": "developer"},
          {"key": "build_pages", "title": "Build pages and CMS", "due_offset_days": 32, "client_visible": true, "assignee_role": "developer"},
          {"key": "qa_review", "title": "QA: devices, forms, performance", "due_offset_days": 35, "client_visible": false, "assignee_role": "developer"}
        ]},
        {"key": "launch", "name": "Launch", "due_offset_days": 42, "tasks": [
          {"key": "client_uat", "title": "Client review and sign-off", "due_offset_days": 39, "client_visible": true, "assignee_role": "account_manager"},
          {"key": "go_live", "title": "Go live and handover", "due_offset_days": 42, "client_visible": true, "assignee_role": "developer"}
        ]}
      ]
    }$json$::jsonb
),
(
    'seo_retainer',
    'SEO Retainer',
    'Audit, technical fixes, content plan and the first monthly report.',
    1,
    $json${
      "schema_version": 1,
      "default_duration_days": 30,
      "milestones": [
        {"key": "onboarding_audit", "name": "Onboarding and audit", "due_offset_days": 10, "tasks": [
          {"key": "access_grant", "title": "Grant Search Console and Analytics access", "due_offset_days": 3, "client_visible": true, "assignee_role": "account_manager"},
          {"key": "technical_audit", "title": "Technical SEO audit", "due_offset_days": 8, "client_visible": true, "assignee_role": "seo_specialist"},
          {"key": "keyword_research", "title": "Keyword and competitor research", "due_offset_days": 10, "client_visible": true, "assignee_role": "seo_specialist"}
        ]},
        {"key": "fixes_and_plan", "name": "Fixes and content plan", "due_offset_days": 21, "tasks": [
          {"key": "technical_fixes", "title": "Implement priority technical fixes", "due_offset_days": 18, "client_visible": true, "assignee_role": "developer"},
          {"key": "content_plan", "title": "90-day content plan", "due_offset_days": 21, "client_visible": true, "assignee_role": "seo_specialist"}
        ]},
        {"key": "first_report", "name": "First monthly report", "due_offset_days": 30, "tasks": [
          {"key": "rank_tracking", "title": "Set up rank tracking", "due_offset_days": 24, "client_visible": false, "assignee_role": "seo_specialist"},
          {"key": "monthly_report", "title": "Monthly report and review call", "due_offset_days": 30, "client_visible": true, "assignee_role": "account_manager"}
        ]}
      ]
    }$json$::jsonb
),
(
    'marketing_automation_setup',
    'Marketing Automation Setup',
    'CRM and tracking setup, automated workflows, QA and launch.',
    1,
    $json${
      "schema_version": 1,
      "default_duration_days": 28,
      "milestones": [
        {"key": "discovery_access", "name": "Discovery and access", "due_offset_days": 5, "tasks": [
          {"key": "process_mapping", "title": "Map the current lead-to-customer process", "due_offset_days": 3, "client_visible": true, "assignee_role": "automation_specialist"},
          {"key": "tool_access", "title": "Collect CRM, email and form tool access", "due_offset_days": 5, "client_visible": true, "assignee_role": "account_manager"}
        ]},
        {"key": "crm_tracking", "name": "CRM and tracking setup", "due_offset_days": 12, "tasks": [
          {"key": "pipeline_setup", "title": "Configure pipeline stages and fields", "due_offset_days": 9, "client_visible": true, "assignee_role": "automation_specialist"},
          {"key": "tracking_setup", "title": "Install tracking and form integrations", "due_offset_days": 12, "client_visible": false, "assignee_role": "developer"}
        ]},
        {"key": "workflows", "name": "Automated workflows", "due_offset_days": 21, "tasks": [
          {"key": "nurture_sequences", "title": "Build lead nurture sequences", "due_offset_days": 17, "client_visible": true, "assignee_role": "automation_specialist"},
          {"key": "internal_alerts", "title": "Set up internal alerts and task routing", "due_offset_days": 21, "client_visible": false, "assignee_role": "automation_specialist"}
        ]},
        {"key": "qa_launch", "name": "QA and launch", "due_offset_days": 28, "tasks": [
          {"key": "end_to_end_test", "title": "End-to-end test with sample leads", "due_offset_days": 25, "client_visible": false, "assignee_role": "automation_specialist"},
          {"key": "training_handover", "title": "Team training and handover", "due_offset_days": 28, "client_visible": true, "assignee_role": "account_manager"}
        ]}
      ]
    }$json$::jsonb
)
ON CONFLICT (key) DO NOTHING;
