{
    'name': 'Configurateur',
    'version': '19.0.1.0.0',
    'summary': "Configurateur de portes et fenêtres PVC, commandes et bons de travail",
    'author': 'Edward IA / KerningCode',
    'category': 'Manufacturing',
    'depends': ['base', 'mail', 'crm', 'sale_management', 'purchase', 'product'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_sequence.xml',
        'data/product_data.xml',
        'views/pff_configuration_views.xml',
        'views/crm_lead_views.xml',
        'views/menus.xml',
    ],
    'application': True,
    'installable': True,
    'license': 'LGPL-3',
}
