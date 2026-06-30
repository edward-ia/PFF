from odoo import models, fields


class PffPoste(models.Model):
    _name = 'pff.poste'
    _description = "Poste de travail (atelier)"
    _order = 'number'

    number = fields.Integer(string='N° de poste', required=True)
    name = fields.Char(string='Nom du poste', required=True)
    type = fields.Selection([
        ('scie', 'Scie'),
        ('poincon', 'Poinçon / machinage'),
        ('soudage', 'Soudage'),
        ('sousens', 'Sous-ensemble'),
        ('assemblage', 'Assemblage'),
        ('achat', 'Achat sur commande'),
    ], string='Type de poste')
    active = fields.Boolean(default=True)


class PffProfil(models.Model):
    _name = 'pff.profil'
    _description = "Profilé / code pièce"
    _order = 'composante, family'

    name = fields.Char(string='Désignation', required=True)
    code = fields.Char(string='Code pièce', required=True)
    composante = fields.Selection([
        ('cadre', 'Cadre'),
        ('volet', 'Volet'),
        ('parclose', 'Parclose'),
        ('meneau', 'Meneau'),
        ('soufflage', 'Soufflage'),
        ('moustiquaire', 'Moustiquaire'),
        ('renfort', 'Renfort acier'),
    ], string='Composante', required=True)
    family = fields.Selection([
        ('battant', 'Battant'),
        ('guillotine', 'Guillotine'),
        ('coulissant', 'Coulissant'),
        ('fixe', 'Fixe'),
        ('all', 'Toutes'),
    ], string='Famille', default='all')
    active = fields.Boolean(default=True)


class PffVerre(models.Model):
    _name = 'pff.verre'
    _description = "Type de verre / thermos (Energy Star)"
    _order = 'name'

    name = fields.Char(string='Type de verre', required=True)
    u_value = fields.Float(string='Valeur U', help="Coefficient de transmission thermique")
    er_value = fields.Float(string='Cote ER', help="Energy Rating")
    thickness = fields.Char(string='Épaisseur')
    kg_m2 = fields.Float(string='Poids (kg/m²)')
    active = fields.Boolean(default=True)
