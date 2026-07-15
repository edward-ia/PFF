import json
import logging
import re

from markupsafe import Markup

from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)

FAMILIES = [
    ('battant', 'Battant'),
    ('guillotine', 'Guillotine simple'),
    ('coulissant', 'Coulissant simple'),
    ('fixe', 'Fixe'),
    ('porte_ext', "Porte d'entrée"),
    ('porte_int', 'Porte intérieure'),
    ('porte_patio', 'Porte patio'),
]

# Correspondance famille du configurateur → référence interne du PRODUIT FABRIQUÉ
# (créé/géré en UI). Sert au « Lancer en fabrication » pour créer l'ordre de
# fabrication du bon produit → sa nomenclature route les OT vers les postes.
FAMILY_PRODUCT_CODE = {
    'battant': 'PFF-BATTANT',
    'guillotine': 'PFF-GUILLOTINE',
    'coulissant': 'PFF-COULISSANT',
    'fixe': 'PFF-FIXE',
    'porte_ext': 'PFF-PORTE-ENT',
    'porte_int': 'PFF-PORTE-INT',
    'porte_patio': 'PFF-PORTE-PATIO',
}


class PffConfiguration(models.Model):
    _name = 'pff.configuration'
    _description = "Commande de produits configurés PFF"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(string='Numéro', required=True, copy=False, readonly=True,
                       default=lambda self: _('Nouveau'))
    partner_id = fields.Many2one('res.partner', string='Client', required=True, tracking=True)
    lead_id = fields.Many2one('crm.lead', string='Opportunité', readonly=True)
    date_order = fields.Datetime(string='Date de création', default=fields.Datetime.now)
    date_delivery = fields.Date(string='Date de livraison')
    state = fields.Selection([
        ('draft', 'Brouillon'),
        ('production', 'En fabrication'),
        ('done', 'Terminé'),
    ], string='Statut', default='draft', tracking=True)
    line_ids = fields.One2many('pff.configuration.line', 'configuration_id',
                               string='Produits configurés')
    currency_id = fields.Many2one('res.currency',
                                  default=lambda self: self.env.company.currency_id)
    amount_total = fields.Monetary(string='Total', compute='_compute_amount_total', store=True)
    product_count = fields.Integer(string='Nb de produits',
                                   compute='_compute_product_count', store=True)
    sale_order_id = fields.Many2one('sale.order', string='Devis', readonly=True, copy=False)
    purchase_order_id = fields.Many2one('purchase.order', string="Bon d'achat verre",
                                        readonly=True, copy=False)
    production_count = fields.Integer(string="Ordres de fabrication",
                                      compute='_compute_production_count')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('Nouveau')) == _('Nouveau'):
                vals['name'] = self.env['ir.sequence'].next_by_code('pff.configuration') or _('Nouveau')
        return super().create(vals_list)

    @api.depends('line_ids.price_subtotal')
    def _compute_amount_total(self):
        for rec in self:
            rec.amount_total = sum(rec.line_ids.mapped('price_subtotal'))

    @api.depends('line_ids')
    def _compute_product_count(self):
        for rec in self:
            rec.product_count = len(rec.line_ids)

    # --- Statuts ---
    def action_production(self):
        for rec in self:
            # A-t-on déjà généré des OF pour cette commande ? (évite les doublons
            # si on repasse le bouton).
            already = self.env['mrp.production'].search_count([('origin', '=', rec.name)])
            rec.write({'state': 'production'})
            # Automatisation : à la mise en fabrication, on crée le bon d'achat
            # verre BROUILLON (silencieux). L'acheteur assigne le fournisseur et valide.
            if not rec.purchase_order_id:
                rec._create_glass_po()
            # Et on lance la fabrication : un OF par produit configuré.
            if not already:
                rec._create_manufacturing_orders()

    def _create_manufacturing_orders(self):
        """Au « Lancer en fabrication » : crée un ordre de fabrication par produit
        configuré, routé vers la nomenclature de sa famille. La nomenclature porte
        les opérations → les ordres de travail tombent automatiquement aux postes."""
        self.ensure_one()
        Product = self.env['product.product']
        Bom = self.env['mrp.bom']
        Production = self.env['mrp.production']
        for line in self.line_ids:
            code = FAMILY_PRODUCT_CODE.get(line.family)
            if not code:
                continue
            product = Product.search([('default_code', '=', code)], limit=1)
            if not product:
                continue
            bom = Bom.search([
                ('product_tmpl_id', '=', product.product_tmpl_id.id),
                '|', ('product_id', '=', product.id), ('product_id', '=', False),
            ], limit=1)
            mo = Production.create({
                'product_id': product.id,
                'product_qty': line.qty or 1,
                'product_uom_id': product.uom_id.id,
                'bom_id': bom.id if bom else False,
                'origin': self.name,
                'pff_line_id': line.id,
            })
            # Routage par morceau AVANT confirmation : en brouillon, les ordres de
            # travail (générés depuis la nomenclature) sont modifiables. On éclate
            # chaque poste en un OT par morceau, routé vers la station choisie au
            # configurateur. Après action_confirm (état ≠ brouillon), Odoo ne
            # recalcule plus les OT → nos modifications sont conservées.
            self._route_line_workorders(mo, line)
            # Retire les OT d'un poste sans rien à faire pour ce produit
            # (ex. Sous-ensemble sans meneau → bon de travail vide).
            self._prune_empty_workorders(mo, line)
            mo.action_confirm()
        return True

    # Relie un poste du configurateur (clé de poste_assign_json : scie, poincon,
    # soudage, sousens, assemblage, achat) au poste de travail réel (mrp.workcenter)
    # via le PRÉFIXE de son nom — robuste aux variantes (« Poinçon/machinage »…).
    _POSTE_CODE_HINT = {
        'scie': 'scie', 'poincon': 'poin', 'soudage': 'soud',
        'sousens': 'sous', 'assemblage': 'assemb', 'validation': 'valid',
    }

    @staticmethod
    def _wc_base_name(name):
        """« Scie 3 » → « Scie » : retire le n° de station en fin de nom."""
        return re.sub(r'\s*\d+\s*$', '', name or '').strip()

    def _poste_code_of_wc(self, name):
        base = self._wc_base_name(name).lower()
        for code, hint in self._POSTE_CODE_HINT.items():
            if base.startswith(hint):
                return code
        return None

    def _route_line_workorders(self, mo, line):
        """Éclate les ordres de travail d'un OF (encore en brouillon) selon les
        stations choisies au configurateur (`poste_assign_json` =
        {"poste|section": "n° station"}). Pour chaque poste ayant AU MOINS un
        choix, l'OT générique est remplacé par UN OT PAR MORCEAU du produit qui
        passe par ce poste : le morceau choisi va à sa station, les autres à la
        station par défaut (celle de la nomenclature, sinon 1) — ainsi aucun
        morceau n'est perdu. Chaque OT porte son morceau dans `pff_section` et
        dans son nom (« coupe — Cadre »). Best-effort et non bloquant : toute
        anomalie laisse l'OF en l'état sans empêcher la mise en fabrication."""
        try:
            assign = json.loads(line.poste_assign_json or '{}')
        except (ValueError, TypeError):
            return
        if not assign:
            return
        # Regroupe les choix par poste : {poste_code: {section: n° station}}
        by_poste = {}
        for key, station in assign.items():
            if not station:
                continue
            parts = key.split('|', 1)
            if len(parts) != 2:
                continue
            by_poste.setdefault(parts[0], {})[parts[1]] = str(station)
        if not by_poste:
            return
        # Morceaux (composantes) réellement présents dans ce produit, dans
        # l'ordre d'affichage du bon de travail.
        try:
            comps = json.loads(line.comps_json or '[]')
        except (ValueError, TypeError):
            comps = []
        grps_present = [g for g in self._BT_ORDER
                        if any((c.get('grp') or '') == g for c in comps)]
        Workcenter = self.env['mrp.workcenter']
        for wo in list(mo.workorder_ids):
            try:
                code = self._poste_code_of_wc(wo.workcenter_id.name)
                secmap = by_poste.get(code)
                if not secmap:
                    continue  # aucun choix pour ce poste → OT générique inchangé
                base = self._wc_base_name(wo.workcenter_id.name)
                # Station par défaut = celle de la nomenclature (n° en fin de
                # nom du poste), sinon « 1 ».
                m = re.search(r'(\d+)\s*$', wo.workcenter_id.name or '')
                default_station = m.group(1) if m else '1'
                # Contrôle qualité : 1 OT par PRODUIT (pas d'éclatement par
                # morceau) → simple réassignation de la station choisie.
                if code == 'validation':
                    station = secmap.get('Produit') or next(iter(secmap.values()), None)
                    if station:
                        wc = Workcenter.search(
                            [('name', '=', '%s %s' % (base, station))], limit=1)
                        if wc:
                            wo.workcenter_id = wc.id
                    continue
                # Sections de CE produit qui passent par ce poste — comparaison
                # par CODE de poste (scie/poincon/…) et non par nom exact, pour
                # rester robuste aux écarts de nommage (« Poinçon/machinage » vs
                # « Poinçon machinage »).
                sections = [g for g in grps_present
                            if code in {self._poste_code_of_wc(p)
                                        for p in self._BT_POSTES.get(g, [])}]
                if not sections:
                    continue
                for section in sections:
                    station = secmap.get(section) or default_station
                    wc = Workcenter.search(
                        [('name', '=', '%s %s' % (base, station))], limit=1
                    ) or wo.workcenter_id
                    wo.copy({
                        'name': '%s — %s' % (wo.name, section),
                        'workcenter_id': wc.id,
                        'pff_section': section,
                    })
                wo.unlink()  # retire l'OT générique, remplacé par les OT par morceau
            except Exception:
                _logger.warning(
                    "PFF: routage par morceau échoué pour l'OT %s (OF %s)",
                    wo.display_name, mo.name, exc_info=True)
        return True

    # Postes dont le contenu est UNIQUEMENT par morceau (pas de contenu produit) :
    # si aucun morceau ne les concerne, leur OT serait vide → à supprimer.
    # (Assemblage = étiquettes, Validation = checklist → toujours du contenu.)
    _MORCEAU_ONLY_POSTES = {'scie', 'poincon', 'soudage', 'sousens'}

    def _prune_empty_workorders(self, mo, line):
        """Supprime les OT génériques d'un poste qui n'a AUCUN morceau à traiter
        pour ce produit (ex. Sous-ensemble sans meneau) → évite d'envoyer un bon
        de travail vide à un poste. Best-effort, non bloquant."""
        try:
            comps = json.loads(line.comps_json or '[]')
        except (ValueError, TypeError):
            comps = []
        grps_present = [g for g in self._BT_ORDER
                        if any((c.get('grp') or '') == g for c in comps)]
        for wo in list(mo.workorder_ids):
            try:
                if wo.pff_section:
                    continue  # OT déjà routé sur un morceau précis → a du contenu
                code = self._poste_code_of_wc(wo.workcenter_id.name)
                if code not in self._MORCEAU_ONLY_POSTES:
                    continue  # Assemblage / Validation / autre → contenu produit
                has_morceau = any(
                    code in {self._poste_code_of_wc(p)
                             for p in self._BT_POSTES.get(g, [])}
                    for g in grps_present)
                if not has_morceau:
                    wo.unlink()
            except Exception:
                _logger.warning(
                    "PFF: purge d'un OT vide échouée (OF %s)", mo.name,
                    exc_info=True)
        return True

    def _compute_production_count(self):
        MO = self.env['mrp.production']
        for rec in self:
            rec.production_count = MO.search_count([('origin', '=', rec.name)]) if rec.name else 0

    def action_view_productions(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Ordres de fabrication',
            'res_model': 'mrp.production',
            'domain': [('origin', '=', self.name)],
            'view_mode': 'list,form',
            'target': 'current',
        }

    def action_done(self):
        self.write({'state': 'done'})

    def action_draft(self):
        self.write({'state': 'draft'})

    # --- Ouvrir le configurateur 3D complet (action client OWL + iframe) ---
    def action_open_configurator(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'pff_configurator',
            'name': 'Configurateur',
            'params': {'config_id': self.id},
            'context': {'config_id': self.id},
        }

    # --- Phase C : créer un devis dans Ventes ---
    def action_create_quotation(self):
        self.ensure_one()
        tmpl = self.env.ref('pff_configurateur.product_pff_configure', raise_if_not_found=False)
        product = tmpl.product_variant_id if tmpl else False
        order = self.env['sale.order'].create({
            'partner_id': self.partner_id.id,
            'origin': self.name,
        })
        for line in self.line_ids:
            self.env['sale.order.line'].create({
                'order_id': order.id,
                'product_id': product.id if product else False,
                'name': line.description or dict(FAMILIES).get(line.family, ''),
                'product_uom_qty': line.qty,
                'price_unit': line.price_unit,
            })
        self.sale_order_id = order.id
        return {
            'type': 'ir.actions.act_window',
            'name': 'Devis',
            'res_model': 'sale.order',
            'res_id': order.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_view_quotation(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': self.sale_order_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # --- Portail distributeur : configurateur → devis brouillon ---
    # Clés de paramètres transmises par le configurateur (miroir exact de la
    # liste blanche de `configurator_action.js`).
    _PORTAL_PARAM_KEYS = [
        'unit', 'cadre', 'verre', 'aspect', 'col_ext', 'col_int', 'moulure',
        'soufflage', 'grilles', 'imposte', 'quinc', 'coupe', 'moust', 'sections',
        'ouvrant', 'vantaux', 'panneau', 'sidelights',
    ]

    def _portal_line_vals(self, d):
        """Mappe un item validé du configurateur en vals de
        `pff.configuration.line`. Miroir du parent back-office
        (`configurator_action.js`, message `pff_valider`)."""
        self.ensure_one()
        vals = {
            'configuration_id': self.id,
            'family': d.get('family'),
            'width': d.get('width'),
            'height': d.get('height'),
            'description': d.get('description'),
            'qty': d.get('qty') or 1,
            'price_unit': d.get('price') or 0,
            'config_json': json.dumps(d.get('config') or {}),
            'thermos_json': json.dumps({
                'glass': d.get('glass') or '',
                'ep': d.get('ep') or '',
                'thermos': d.get('thermos') or [],
            }),
            'comps_json': json.dumps(d.get('comps') or []),
            'poste_assign_json': json.dumps(d.get('poste_assign') or {}),
        }
        params = d.get('params') or {}
        for k in self._PORTAL_PARAM_KEYS:
            v = params.get(k)
            vals['param_' + k] = '' if v is None else str(v)
        return vals

    @api.model
    def _portal_create_from_items(self, partner, items):
        """Crée une configuration + ses lignes + un devis BROUILLON pour le
        distributeur `partner`, à partir des items validés au configurateur.
        Appelé en `sudo` par le contrôleur portail (le partenaire est déjà forcé
        sur la société du distributeur connecté — jamais un ID venu du client)."""
        config = self.create({'partner_id': partner.id})
        Line = self.env['pff.configuration.line']
        for d in (items or []):
            if not d.get('family'):
                continue
            Line.create(config._portal_line_vals(d))
        order = config._portal_create_quotation(partner)
        return config, order

    def _portal_create_quotation(self, partner):
        """Comme `action_create_quotation`, mais rend le devis visible et signable
        au portail (le distributeur en devient suiveur, état « envoyé »)."""
        self.ensure_one()
        tmpl = self.env.ref('pff_configurateur.product_pff_configure', raise_if_not_found=False)
        product = tmpl.product_variant_id if tmpl else False
        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'origin': self.name,
        })
        for line in self.line_ids:
            self.env['sale.order.line'].create({
                'order_id': order.id,
                'product_id': product.id if product else False,
                'name': line.description or dict(FAMILIES).get(line.family, ''),
                'product_uom_qty': line.qty,
                'price_unit': line.price_unit,
            })
        self.sale_order_id = order.id
        # Rendre le devis VISIBLE et SIGNABLE par le distributeur au portail :
        # un brouillon reste interne (Odoo le cache au client) → on abonne le
        # distributeur comme suiveur et on passe le devis en « envoyé » (sent).
        order.message_subscribe(partner_ids=partner.commercial_partner_id.ids)
        if order.state == 'draft':
            order.write({'state': 'sent'})
        return order

    # --- Phase D : bon d'achat verre (thermos) créé automatiquement en fabrication ---
    def _create_glass_po(self):
        """Crée un purchase.order BROUILLON avec une ligne par thermos (mesures
        capturées par le configurateur → aucun recalcul). Silencieux : renvoie
        None sans bloquer s'il n'y a pas de vitrage. Le FOURNISSEUR est lu depuis
        le produit Thermos (onglet Achats), que l'utilisateur configure lui-même
        (ex. « Multiver »). Si aucun fournisseur n'y est réglé, le bon d'achat est
        simplement sauté (non bloquant)."""
        self.ensure_one()
        tmpl = self.env.ref('pff_configurateur.product_pff_thermos',
                            raise_if_not_found=False)
        product = tmpl.product_variant_id if tmpl else False
        if not product:
            return False
        po_lines = []
        for item_no, line in enumerate(self.line_ids, 1):
            if not line.thermos_json:
                continue
            try:
                data = json.loads(line.thermos_json)
            except (ValueError, TypeError):
                continue
            glass = data.get('glass') or 'Thermos'
            ep = data.get('ep') or ''
            ep_s = ('%smm' % ep) if (ep and 'mm' not in str(ep)) else (ep or '')
            for t in data.get('thermos', []):
                qty = (t.get('qte') or 1) * (line.qty or 1)
                # Format Fusion : (commande/item) verre (L X H) entretoise, Epais Total.
                name = "(%s/%s) %s (%s X %s) Technoform Noir, Epais Total: %s." % (
                    self.name or '', item_no, glass, t.get('w'), t.get('h'), ep_s,
                )
                # product_uom / date_planned / price_unit : laissés aux calculs
                # Odoo (dérivés du produit) pour rester compatible Odoo 19.
                po_lines.append((0, 0, {
                    'product_id': product.id,
                    'name': name,
                    'product_qty': qty,
                }))
        if not po_lines:
            return False
        # Fournisseur = celui configuré sur le produit Thermos (onglet Achats).
        # Master data géré par l'utilisateur (il y met « Multiver »). Purchase.order
        # EXIGE un fournisseur ; si aucun n'est configuré, on saute le bon d'achat
        # SANS bloquer la mise en fabrication (les OF se créent quand même).
        vendor = product.seller_ids[:1].partner_id
        if not vendor:
            return False
        po = self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'origin': self.name,
            'order_line': po_lines,
        })
        self.purchase_order_id = po.id
        return po

    def action_view_purchase(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order',
            'res_id': self.purchase_order_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ------------------------------------------------------------------
    #  RAPPORT « Commande client » (format Fusion) — données dynamiques
    # ------------------------------------------------------------------
    # ---- Dessin 2D « vue extérieure » façon Fusion (SVG procédural) ----
    _SLIDE_FAMS = ('coulissant', 'porte_patio')

    def _svg_frame(self, x, y, w, h, ft):
        """Cadre biseauté (bande grise + onglets d'about + verre clair).
        Retourne (liste de parties SVG, rect intérieur (ix, iy, iw, ih))."""
        ix, iy, iw, ih = x + ft, y + ft, w - 2 * ft, h - 2 * ft
        p = [
            '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#cbcfd4" '
            'stroke="#6b6f74" stroke-width="0.8"/>' % (x, y, w, h),
            '<path d="M%.1f %.1f L%.1f %.1f M%.1f %.1f L%.1f %.1f '
            'M%.1f %.1f L%.1f %.1f M%.1f %.1f L%.1f %.1f" stroke="#9aa0a6" '
            'stroke-width="0.5" fill="none"/>' % (
                x, y, ix, iy, x + w, y, ix + iw, iy,
                x + w, y + h, ix + iw, iy + ih, x, y + h, ix, iy + ih),
            '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#eef1f5" '
            'stroke="#7c8085" stroke-width="0.6"/>' % (ix, iy, iw, ih),
        ]
        return p, (ix, iy, iw, ih)

    def _svg_arrow_h(self, x1, x2, y, hw=3.2):
        d = 1.0 if x2 >= x1 else -1.0
        return ('<path d="M%.1f %.1f L%.1f %.1f M%.1f %.1f L%.1f %.1f L%.1f %.1f" '
                'fill="none" stroke="#111" stroke-width="1.9" stroke-linejoin="round" '
                'stroke-linecap="round"/>' % (
                    x1, y, x2, y, x2 - d * hw, y - hw, x2, y, x2 - d * hw, y + hw))

    def _svg_arrow_v(self, y1, y2, x, hw=3.2):
        d = 1.0 if y2 >= y1 else -1.0
        return ('<path d="M%.1f %.1f L%.1f %.1f M%.1f %.1f L%.1f %.1f L%.1f %.1f" '
                'fill="none" stroke="#111" stroke-width="1.9" stroke-linejoin="round" '
                'stroke-linecap="round"/>' % (
                    x, y1, x, y2, x - hw, y2 - d * hw, x, y2, x + hw, y2 - d * hw))

    def _cmd_svg(self, fam, nsec, right, w_in=1, h_in=1, idx=0):
        """Schéma « vue extérieure » façon Fusion : cadre biseauté à l'échelle du
        produit, refends par section et symbole d'ouverture (triangle = battant,
        flèche horizontale = coulissant/patio, flèche verticale = guillotine)."""
        max_w, max_h = 132.0, 92.0
        ratio = (w_in or 1) / float(h_in or 1)
        if max_w / max_h > ratio:
            dh = max_h
            dw = dh * ratio
        else:
            dw = max_w
            dh = dw / ratio
        m = 2.5
        canv_w, canv_h = dw + 2 * m, dh + 2 * m
        ft = max(2.4, min(dw, dh) * 0.055)
        s = ['<svg viewBox="0 0 %.1f %.1f" width="%.1f" height="%.1f" '
             'xmlns="http://www.w3.org/2000/svg">' % (canv_w, canv_h, canv_w, canv_h)]
        outer, (ix, iy, iw, ih) = self._svg_frame(m, m, dw, dh, ft)
        s += outer
        ft2 = max(1.8, ft * 0.62)
        g = 1.2  # jeu entre sous-cadres

        if fam in self._SLIDE_FAMS and nsec >= 2:
            secw = iw / nsec
            op = 0 if right else nsec - 1  # section ouvrante
            for k in range(nsec):
                sx = ix + k * secw + (g if k else 0)
                sw = secw - (g if k else 0) - (g if k < nsec - 1 else 0)
                sub, (jx, jy, jw, jh) = self._svg_frame(sx, iy, sw, ih, ft2)
                s += sub
                if k == op:
                    cy = jy + jh / 2
                    if right:
                        s.append(self._svg_arrow_h(jx + jw * 0.30, jx + jw * 0.70, cy))
                    else:
                        s.append(self._svg_arrow_h(jx + jw * 0.70, jx + jw * 0.30, cy))
        elif fam == 'guillotine':
            top_h = ih * 0.36
            sub_t, _ = self._svg_frame(ix, iy, iw, top_h - g, ft2)
            sub_b, (bx, by, bw, bh) = self._svg_frame(
                ix, iy + top_h + g, iw, ih - top_h - g, ft2)
            s += sub_t + sub_b
            s.append(self._svg_arrow_v(by + bh * 0.72, by + bh * 0.30, bx + bw / 2))
        elif fam == 'battant':
            cx = ix + iw / 2
            s.append('<path d="M%.1f %.1f L%.1f %.1f L%.1f %.1f" fill="none" '
                     'stroke="#111" stroke-width="1.6" stroke-linejoin="round"/>' % (
                         ix + iw * 0.06, iy + ih * 0.94, cx, iy + ih * 0.06,
                         ix + iw * 0.94, iy + ih * 0.94))
        elif fam in ('porte_ext', 'porte_int'):
            s.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="none" '
                     'stroke="#9aa0a6" stroke-width="0.8"/>' % (
                         ix + iw * 0.14, iy + ih * 0.10, iw * 0.72, ih * 0.80))
        # fixe : cadre seul, aucun symbole
        s.append('</svg>')
        return Markup(''.join(s))

    def _fmt_money(self, v):
        """Montant au format québécois : « 1 700,00 $ » (espace milliers, virgule)."""
        s = '%.2f' % (v or 0.0)
        intp, dec = s.split('.')
        neg = intp.startswith('-')
        if neg:
            intp = intp[1:]
        grp = ''
        while len(intp) > 3:
            grp = ' ' + intp[-3:] + grp
            intp = intp[:-3]
        return ('-' if neg else '') + intp + grp + ',' + dec + ' $'

    def _fmt_mm(self, v):
        """Dimension mm façon Fusion : « 914,4 » (virgule) ou « 762 » si entier."""
        r = round(v or 0.0, 1)
        if abs(r - round(r)) < 0.05:
            return str(int(round(r)))
        return ('%.1f' % r).replace('.', ',')

    def _get_commande_data(self):
        """Assemble les données de la commande client au format Fusion."""
        self.ensure_one()

        def mm2in(mm):
            return round((mm or 0) / 25.4)

        fam_labels = dict(FAMILIES)
        items = []
        for idx, l in enumerate(self.line_ids, 1):
            try:
                nsec = int(''.join(c for c in (l.param_sections or '') if c.isdigit()) or 1)
            except ValueError:
                nsec = 1
            nsec = max(1, nsec)
            w_in, h_in = mm2in(l.width), mm2in(l.height)
            right = (l.param_ouvrant or 'D') != 'G'

            # Nom façon Fusion : « Coulissant Simple PVC / N sections »
            name = "%s PVC / %s sections" % (fam_labels.get(l.family, l.family).title(), nsec)
            ratio = "Horizontal: " + " - ".join(["1/%d" % nsec] * nsec)

            # Dimensions par section (mm, dérivées des pouces comme Fusion)
            sec_w = (w_in / float(nsec)) * 25.4
            sec_h = h_in * 25.4
            section = " , ".join(
                ["(%sx%s)" % (self._fmt_mm(sec_w), self._fmt_mm(sec_h))] * nsec)

            # COUPE-FROID sur ce qui glisse ; QUINCAILLERIE sur le battant (convention Fusion)
            slider = l.family in self._SLIDE_FAMS or l.family == 'guillotine'
            egress = 'egress' in (l.description or '').lower()

            specs = []
            if l.param_cadre:
                specs.append("CADRE : %s (%s x %s)" % (l.param_cadre, w_in, h_in))
            specs.append("SECTION : %s " % section)
            if l.param_verre:
                specs.append("VERRE : %s, Technoform Noir" % l.param_verre)
            if l.param_grilles and l.param_grilles not in ('Aucun', 'none', ''):
                specs.append("CROISILLONS : %s" % l.param_grilles)
            if l.param_soufflage and l.param_soufflage not in ('Aucun', 'none', ''):
                specs.append('FINITION INT.: Ext PVC %s Fermée (9876-1100), '
                             'Épais (totale): 10", Env. à part' % l.param_soufflage)
            if egress:
                specs.append("*RESPECTE LA NORME EGRESS")
            if l.param_moust and l.param_moust not in ('Non', 'none', ''):
                specs.append("MOUSTIQUAIRE : Fibre de verre")
            if not slider and l.param_quinc:
                specs.append("QUINCAILLERIE : %s" % l.param_quinc)
            if slider and l.param_coupe:
                specs.append("COUPE-FROID : %s" % l.param_coupe)

            items.append({
                'idx': idx,
                'name': name,
                'ratio': ratio,
                'w_in': w_in, 'h_in': h_in, 'qty': l.qty,
                'price_unit_f': self._fmt_money(l.price_unit),
                'subtotal_f': self._fmt_money(l.price_subtotal),
                'svg': self._cmd_svg(l.family, nsec, right, w_in, h_in, idx),
                'specs': specs,
            })
        sub = self.amount_total
        tps = round(sub * 0.05, 2)
        tvq = round(sub * 0.09975, 2)
        total = round(sub + tps + tvq, 2)
        return {'items': items,
                'sub_f': self._fmt_money(sub), 'tps_f': self._fmt_money(tps),
                'tvq_f': self._fmt_money(tvq), 'total_f': self._fmt_money(total),
                'po': (self.sale_order_id.client_order_ref or '') if self.sale_order_id else ''}

    # --- Bons de travail (feuilles de production) : données pour le rapport QWeb ---
    # Routage composante → postes (chevrons), et ordre d'affichage des feuilles.
    _BT_POSTES = {
        'Cadre': ['Scie', 'Poinçon machinage', 'Soudage'],
        'Volet': ['Scie', 'Poinçon machinage', 'Soudage'],
        'Parclose': ['Scie', 'Poinçon machinage'],
        'Meneau': ['Scie', 'Poinçon machinage', 'Sous-ensemble'],
        'Renfort acier': ['Scie', 'Soudage'],
        'Soufflage': ['Scie', 'Assemblage'],
        'Moustiquaire': ['Scie', 'Assemblage'],
        'Moulure': ['Scie', 'Assemblage'],
        'Croisillons': ['Scie', 'Assemblage'],
    }
    _BT_ORDER = ['Cadre', 'Volet', 'Parclose', 'Meneau', 'Renfort acier',
                 'Soufflage', 'Moustiquaire', 'Moulure', 'Croisillons']
    _BT_SECTION = {
        'battant': 'A', 'guillotine': 'GS', 'coulissant': 'CSG,F', 'fixe': 'F',
        'porte_ext': 'PE', 'porte_int': 'PI', 'porte_patio': 'PP',
    }
    _BT_POSTE_COLORS = {
        'Scie': '#E8720C', 'Poinçon machinage': '#E8720C', 'Soudage': '#2E9BD6',
        'Sous-ensemble': '#C463C9', 'Assemblage': '#C463C9',
        'Achat sur commande': '#7A7A7A', 'Validation': '#059669',
    }

    def _bt_poste_band(self, postes):
        """Bande de postes en CHEVRONS (flèches emboîtées) façon Fusion, en SVG
        (fiable en PDF, contrairement à clip-path). Chaque poste = une flèche
        colorée pointant vers le suivant."""
        if not postes:
            return Markup('')
        seg_w, h, p = 150.0, 30.0, 15.0  # largeur segment, hauteur, profondeur pointe
        total = len(postes) * seg_w
        s = ['<svg viewBox="0 0 %.0f %.0f" width="%.0f" height="%.0f" '
             'xmlns="http://www.w3.org/2000/svg" '
             'font-family="Arial,Helvetica,sans-serif">' % (total, h, total, h)]
        for i, poste in enumerate(postes):
            x = i * seg_w
            color = self._BT_POSTE_COLORS.get(poste, '#888888')
            if i == 0:  # bord gauche droit, pointe à droite
                path = 'M%.1f 0 L%.1f 0 L%.1f %.1f L%.1f %.1f L%.1f %.1f Z' % (
                    x, x + seg_w - p, x + seg_w, h / 2, x + seg_w - p, h, x, h)
            else:  # encoche à gauche (s'emboîte), pointe à droite
                path = ('M%.1f 0 L%.1f 0 L%.1f %.1f L%.1f %.1f L%.1f %.1f L%.1f %.1f Z'
                        % (x, x + seg_w - p, x + seg_w, h / 2, x + seg_w - p, h,
                           x, h, x + p, h / 2))
            s.append('<path d="%s" fill="%s"/>' % (path, color))
            s.append('<text x="%.1f" y="%.1f" fill="#fff" font-size="12" '
                     'font-weight="bold" text-anchor="middle">%s</text>' % (
                         x + seg_w / 2 + (p / 2 if i else 0), h * 0.63, poste))
        s.append('</svg>')
        return Markup(''.join(s))

    def _get_bt_data(self, only_line=None):
        """Prépare les données du bon de travail pour le rapport QWeb :
        feuilles de production par composante (agrégées depuis les listes de
        coupe capturées), thermos, étiquettes d'assemblage, validation.
        `only_line` : si fourni (une pff.configuration.line), ne garde que ce
        produit — l'index d'item (COMM|ITEM) reste sa vraie position dans la
        commande. Sert au bon de travail de station (1 OF = 1 produit)."""
        self.ensure_one()
        fam_labels = dict(FAMILIES)

        def _load(txt, default):
            try:
                return json.loads(txt) if txt else default
            except (ValueError, TypeError):
                return default

        # Feuilles de production par composante
        feuilles = []
        for grp in self._BT_ORDER:
            rows = []
            for idx, line in enumerate(self.line_ids, start=1):
                if only_line and line.id != only_line.id:
                    continue
                for c in _load(line.comps_json, []):
                    if (c.get('grp') or '') == grp:
                        rows.append({
                            'item': idx,
                            'code': c.get('code') or '',
                            'desc': c.get('desc') or '',
                            'casier': c.get('case') or c.get('casier') or c.get('bac') or '',
                            'tiger': c.get('lng'),
                            'qte': (c.get('qte') or 1) * (line.qty or 1),
                        })
            if rows:
                feuilles.append({
                    'name': grp.upper(),
                    'postes': self._BT_POSTES.get(grp, ['Scie']),
                    'rows': rows,
                })

        # Thermos (achat)
        thermos = []
        for idx, line in enumerate(self.line_ids, start=1):
            if only_line and line.id != only_line.id:
                continue
            data = _load(line.thermos_json, {})
            for t in data.get('thermos', []):
                thermos.append({
                    'item': idx,
                    'desc': '%s %s × %s mm (ép. %s)' % (
                        data.get('glass') or 'Thermos', t.get('w'), t.get('h'),
                        data.get('ep') or ''),
                    'qte': (t.get('qte') or 1) * (line.qty or 1),
                })

        # Étiquettes d'assemblage + validation
        etiquettes, validation = [], []
        num = ''.join(ch for ch in (self.name or '') if ch.isdigit()) or '0'
        # Client RÉEL — même source que le devis (fiche client), aucune valeur figée.
        client_name = self.partner_id.name or ''
        client_loc = ' '.join(x for x in [self.partner_id.city or '',
                                          self.partner_id.state_id.code or ''] if x)
        for idx, line in enumerate(self.line_ids, start=1):
            if only_line and line.id != only_line.id:
                continue
            # Code de profilé du VOLET, repris de la liste de coupe capturée
            # (ligne « VOLET : No article … » du vrai format Fusion).
            volet_code = next(
                (c.get('code') for c in _load(line.comps_json, [])
                 if (c.get('grp') or '') == 'Volet' and c.get('code')), '')
            try:
                nsec = max(1, int(''.join(ch for ch in (line.param_sections or '')
                                          if ch.isdigit()) or 1))
            except ValueError:
                nsec = 1
            bullets = []
            if line.param_cadre:
                bullets.append('CADRE : %s' % line.param_cadre)
            if volet_code:
                bullets.append('VOLET : No article : %s' % volet_code)
            if line.family == 'guillotine':
                bullets.append('BALANCE : RESSORT 5LBS COURT + RESSORT 6LBS MOYEN')
            if line.param_verre:
                bullets.append('VERRE : %s, Technoform Noir' % line.param_verre)
            if line.param_soufflage and line.param_soufflage not in ('Aucun', 'Aucune', 'Non', ''):
                bullets.append('FINITION INT. : %s' % line.param_soufflage)
            if line.param_moust and line.param_moust not in ('Non', 'Aucune', ''):
                bullets.append('MOUSTIQUAIRE : %s' % line.param_moust)
            if line.param_coupe:
                bullets.append('COUPE-FROID : %s' % line.param_coupe)
            if line.param_quinc:
                bullets.append('QUINCAILLERIE : %s' % line.param_quinc)
            w_in = int(round((line.width or 0) / 25.4))
            h_in = int(round((line.height or 0) / 25.4))
            etiquettes.append({
                'item': idx,
                'commande': self.name or '',
                'client': client_name,
                'client_loc': client_loc,
                'name': '%s PVC / %s section(s) %s' % (
                    fam_labels.get(line.family, line.family), nsec,
                    self._BT_SECTION.get(line.family, '')),
                'dim': '%s x %s mm.' % (self._fmt_mm(line.width), self._fmt_mm(line.height)),
                # Dessin « vue extérieure » — MÊME source que le devis (_cmd_svg).
                'svg': self._cmd_svg(line.family, nsec,
                                     (line.param_ouvrant or 'D') != 'G',
                                     w_in, h_in, idx),
                'type': fam_labels.get(line.family, line.family),
                'section': self._BT_SECTION.get(line.family, ''),
                'width': int(line.width or 0),
                'height': int(line.height or 0),
                'family': line.family,
                'bullets': bullets,
                'barcode': 'S%s%02dTA' % (num[-4:].zfill(4), idx),
            })
            validation.append({
                'item': idx,
                'type': fam_labels.get(line.family, line.family),
                'width': int(line.width or 0),
                'height': int(line.height or 0),
                'egress': 'egress' in (line.description or '').lower(),
            })

        return {'feuilles': feuilles, 'thermos': thermos,
                'etiquettes': etiquettes, 'validation': validation}

    # Points de contrôle du produit fini (poste Validation / Contrôle qualité).
    _QC_CHECKLIST = [
        'Dimensions conformes (largeur × hauteur)',
        'Équerrage — diagonales égales',
        'Vitrage : thermos conforme, sans rayure ni bris',
        'Quincaillerie : opération fluide et verrouillage',
        'Étanchéité des joints',
        'Finition : couleur et surface sans défaut',
        'Moustiquaire posée (si applicable)',
        'Identification / étiquette apposée',
        'Nettoyage et emballage — prêt à expédier',
    ]

    def _bt_validation_data(self, line):
        """Données du bon de Contrôle qualité : produit fini + checklist."""
        fam_labels = dict(FAMILIES)
        prod = {
            'type': fam_labels.get(line.family, line.family) if line else '',
            'width': int(line.width or 0) if line else 0,
            'height': int(line.height or 0) if line else 0,
        }
        return {'product': prod, 'checklist': list(self._QC_CHECKLIST)}

    def _bt_data_for_workorder(self, wo):
        """Ordre de travail PAR STATION (mrp.workorder) : ne garde que
        (1) le produit de CET OF (via `mo.pff_line_id`) — pas toute la commande,
        et (2) le morceau routé vers CETTE station (`wo.pff_section`, sinon
        déduit du nom « … — Cadre »). Si l'OT est générique (aucun morceau), on
        garde toutes les composantes du poste. Le thermos reste joint à
        l'Assemblage. Le poste Validation = contrôle qualité du produit fini
        (checklist, pas de morceau). `base` = poste sans le n° de station."""
        self.ensure_one()
        base = self._wc_base_name(wo.workcenter_id.name).replace('/', ' ')
        section = wo.pff_section or ''
        if not section and ' — ' in (wo.name or ''):
            section = wo.name.rsplit(' — ', 1)[1]
        only_line = wo.production_id.pff_line_id or None
        # Contrôle qualité : pas de pièce à couper → checklist du produit fini.
        if base == 'Validation':
            return {'poste': base, 'section': '', 'feuilles': [], 'thermos': [],
                    'validation': self._bt_validation_data(only_line)}
        data = self._get_bt_data(only_line=only_line)
        feuilles = [f for f in data['feuilles'] if base in f['postes']]
        if section:
            feuilles = [f for f in feuilles if f['name'] == section.upper()]
        thermos = data['thermos'] if base in ('Assemblage', 'Achat sur commande') else []
        return {'poste': base, 'section': section, 'feuilles': feuilles,
                'thermos': thermos, 'validation': None}


class PffConfigurationLine(models.Model):
    _name = 'pff.configuration.line'
    _description = "Produit configuré PFF"
    _order = 'sequence, id'

    configuration_id = fields.Many2one('pff.configuration', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    family = fields.Selection(FAMILIES, string='Famille', required=True, default='coulissant')
    width = fields.Float(string='Largeur (mm)', default=1219)
    height = fields.Float(string='Hauteur (mm)', default=914)
    description = fields.Char(string='Désignation')
    config_json = fields.Text(string='Paramètres (JSON)')   # tout l'état du configurateur
    thermos_json = fields.Text(string='Thermos (JSON)')     # verre capturé pour le bon d'achat
    comps_json = fields.Text(string='Liste de coupe (JSON)')  # pièces capturées pour les feuilles de production
    poste_assign_json = fields.Text(string='Stations par morceau (JSON)')  # {"poste|section": "n° station"} → routage des OT
    qty = fields.Integer(string='Qté', default=1)
    price_unit = fields.Float(string='Prix unitaire')
    price_subtotal = fields.Float(string='Sous-total', compute='_compute_subtotal', store=True)
    currency_id = fields.Many2one(related='configuration_id.currency_id')

    # --- Paramètres détaillés (libellés lisibles remplis par le configurateur) ---
    # Copies dénormalisées de config_json, uniquement pour l'affichage en
    # colonnes optionnelles dans la liste. Source de vérité = config_json.
    param_unit = fields.Char(string="Unité")
    param_cadre = fields.Char(string="Cadre")
    param_verre = fields.Char(string="Verre / Thermos")
    param_aspect = fields.Char(string="Aspect du verre")
    param_col_ext = fields.Char(string="Couleur ext.")
    param_col_int = fields.Char(string="Couleur int.")
    param_moulure = fields.Char(string="Moulure ext.")
    param_soufflage = fields.Char(string="Soufflage int.")
    param_grilles = fields.Char(string="Croisillons")
    param_imposte = fields.Char(string="Imposte")
    param_quinc = fields.Char(string="Quincaillerie")
    param_coupe = fields.Char(string="Coupe-froid")
    param_moust = fields.Char(string="Moustiquaire")
    param_sections = fields.Char(string="Sections")
    param_ouvrant = fields.Char(string="Sens d'ouverture")
    param_vantaux = fields.Char(string="Vantaux")
    param_panneau = fields.Char(string="Type de panneau")
    param_sidelights = fields.Char(string="Verres latéraux")

    @api.depends('qty', 'price_unit')
    def _compute_subtotal(self):
        for line in self:
            line.price_subtotal = line.qty * line.price_unit

    def action_edit_line(self):
        """Reprendre la configuration de CE produit précis : ouvre le
        configurateur sur cette ligne ; seule cette ligne sera remplacée
        à la validation (les autres produits restent intacts)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'pff_configurator',
            'name': 'Configurateur',
            'params': {'config_id': self.configuration_id.id, 'line_id': self.id},
            'context': {'config_id': self.configuration_id.id, 'line_id': self.id},
        }


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    # Relie l'OF à la ligne configurée dont il est issu (1 OF par produit
    # configuré). Permet au bon de travail de station de n'afficher QUE le
    # produit de cet OF, pas toute la commande.
    pff_line_id = fields.Many2one(
        'pff.configuration.line', string="Ligne configurée PFF", index=True,
        ondelete='set null',
        help="Ligne du configurateur PFF à l'origine de cet ordre de fabrication.")


class MrpWorkorder(models.Model):
    _inherit = 'mrp.workorder'

    # Morceau (Cadre, Volet, Parclose…) routé vers cette station par le
    # configurateur. Permet au rapport de station de ne montrer que ce morceau.
    pff_section = fields.Char(
        string="Morceau PFF",
        help="Composante routée vers cette station par le configurateur PFF.")
