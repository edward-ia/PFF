{
    'name': 'Configurateur',
    'version': '19.0.1.3.5',
    'summary': "Configurateur de portes et fenêtres PVC, commandes et bons de travail",
    'author': 'Edward IA',
    'category': 'Manufacturing',
    'depends': ['base', 'mail', 'crm', 'sale_management', 'purchase', 'product',
                'mrp', 'portal', 'website'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_sequence.xml',
        'data/product_data.xml',
        'views/pff_configuration_views.xml',
        'views/pff_settings_views.xml',
        'views/crm_lead_views.xml',
        'views/menus.xml',
        'report/report_pff.xml',
        'views/sale_portal_templates.xml',
        'views/portal_configurateur_templates.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'pff_configurateur/static/src/configurator_action.js',
        ],
        'web.assets_frontend': [
            'pff_configurateur/static/src/portal_configurator.js',
        ],
    },
    'application': True,
    'installable': True,
    'license': 'LGPL-3',
}
