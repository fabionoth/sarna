import os

from PIL import Image as PillowImage
from flask import Blueprint, render_template, request, flash, send_from_directory
from flask import abort
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from sarna.auxiliary import redirect_back, redirect_endpoint
from sarna.core.auth import login_required, current_user
from sarna.core.security import limiter
from sarna.forms.assessment import AssessmentForm, FindingEditForm, ActiveCreateNewForm, EvidenceCreateNewForm
from sarna.model import *
from sarna.model.enums import FindingStatus
from sarna.report_generator.engine import generate_reports_bundle

ROUTE_NAME = os.path.basename(__file__).split('.')[0]
blueprint = Blueprint('assessments', __name__)


@blueprint.route('/')
@login_required
def index():
    assessments = Assessment.query.filter(
        (Assessment.creator == current_user) |
        (Assessment.client_id.in_(map(lambda x: x.id, current_user.managed_clients))) |
        (Assessment.client_id.in_(map(lambda x: x.id, current_user.audited_clients))) |
        (Assessment.auditors.any(User.id == current_user.id))
    )
    context = dict(
        route=ROUTE_NAME,
        assessments=assessments
    )
    return render_template('assessments/list.html', **context)


@blueprint.route('/<assessment_id>', methods=('GET', 'POST'))
@login_required
def edit(assessment_id):
    assessment: Assessment = Assessment.query.filter_by(id=assessment_id).one()
    if not current_user.owns(assessment):
        abort(403)

    if request.form:
        form = AssessmentForm(request.form)
    else:
        form = AssessmentForm(**assessment.to_dict(), auditors=assessment.auditors)

    form.auditors.choices = User.choices()

    context = dict(
        route=ROUTE_NAME,
        assessment=assessment,
        form=form
    )

    if form.validate_on_submit():
        data = dict(form.data)
        data.pop('csrf_token', None)
        auditors = data.pop('auditors', [])

        assessment.set(**data)
        assessment.auditors.clear()
        assessment.auditors.extend(auditors)

        return redirect_back('.index')

    return render_template('assessments/edit.html', **context)


@blueprint.route('/<assessment_id>/delete', methods=('POST',))
@login_required
def delete(assessment_id):
    assessment = Assessment.query.filter_by(id=assessment_id).one()
    if not current_user.owns(assessment):
        abort(403)

    assessment.delete()
    return redirect_back('.index')


@blueprint.route('/<assessment_id>/summary')
@login_required
def summary(assessment_id):
    assessment = Assessment.query.filter_by(id=assessment_id).one()
    if not current_user.audits(assessment):
        abort(403)

    context = dict(
        route=ROUTE_NAME,
        endpoint=request.url_rule.endpoint.split('.')[-1],
        assessment=assessment
    )
    return render_template('assessments/panel/summary.html', **context)


@blueprint.route('/<assessment_id>/findings/resource/<affected_resource_id>')
@blueprint.route('/<assessment_id>/findings')
@login_required
def findings(assessment_id, affected_resource_id=None):
    assessment = Assessment.query.filter_by(id=assessment_id).one()
    if not current_user.audits(assessment):
        abort(403)

    context = dict(
        route=ROUTE_NAME,
        endpoint='findings',
        assessment=assessment
    )

    if affected_resource_id is not None:
        affected = AffectedResource.query.filter_by(id=affected_resource_id).one()
        if affected.active.assessment != assessment:
            return abort(401)

        list_findings = affected.findings
    else:
        list_findings = assessment.findings

    context['findings'] = list_findings
    return render_template('assessments/panel/list_findings.html', **context)


@blueprint.route('/<assessment_id>/findings/<finding_id>', methods=('GET', 'POST'))
@login_required
def edit_finding(assessment_id, finding_id):
    assessment = Assessment.query.filter_by(id=assessment_id).one()
    if not current_user.audits(assessment):
        abort(403)

    finding = Finding.query.filter_by(id=finding_id).one()

    finding_dict = finding.to_dict()
    finding_dict['affected_resources'] = "\r\n".join(r.uri for r in finding.affected_resources)
    form_data = request.form.to_dict() or finding_dict
    form = FindingEditForm(**form_data)
    context = dict(
        route=ROUTE_NAME,
        endpoint='findings',
        assessment=assessment,
        form=form,
        finding=finding,
        solutions=finding.template.solutions,
        solutions_dict={
            a.name: a.text
            for a in finding.template.solutions
        }
    )
    if form.validate_on_submit():
        data = dict(form.data)
        data.pop('csrf_token', None)
        affected_resources = data.pop('affected_resources', '').split('\n')
        try:
            finding.update_affected_resources(affected_resources)  # TODO: Raise different exception
            finding.set(**data)
            return redirect_back('.findings', assessment_id=assessment_id)
        except ValueError as ex:
            form.affected_resources.errors.append(str(ex))

    return render_template('assessments/panel/edit_finding.html', **context)


@blueprint.route('/<assessment_id>/findings/<finding_id>/delete', methods=('POST',))
@login_required
def delete_findings(assessment_id, finding_id):
    finding = Finding.query.filter_by(id=finding_id).one()
    if not current_user.audits(finding.assessment):
        abort(403)

    db.session.delete(finding)
    db.session.commit()
    flash("Findign deleted", "success")
    return redirect_back('.findings', assessment_id=assessment_id)


@blueprint.route('/<assessment_id>/add')
@login_required
def add_findings(assessment_id):
    assessment = Assessment.query.filter_by(id=assessment_id).one()
    if not current_user.audits(assessment):
        abort(403)

    context = dict(
        route=ROUTE_NAME,
        endpoint=request.url_rule.endpoint.split('.')[-1],
        assessment=assessment,
        findings=FindingTemplate.query.all()
    )
    return render_template('assessments/panel/add_finding.html', **context)


@blueprint.route('/<assessment_id>/add/<finding_id>', methods=('POST',))
@login_required
def add_finding(assessment_id, finding_id):
    action = request.form.get('action', None)
    if not action or action not in {'add', 'edit_add'}:
        abort(400)

    assessment = Assessment.query.filter_by(id=assessment_id).one()
    if not current_user.audits(assessment):
        abort(403)

    template = FindingTemplate.query.filter_by(id=finding_id).one()

    finding = Finding.build_from_template(template, assessment)

    db.session.commit()
    flash('Finding {} added successfully'.format(finding.name), 'success')

    if action == 'edit_add':
        return redirect_endpoint('.edit_finding', assessment_id=assessment.id, finding_id=finding.id)

    return redirect_back('.add_findings', assessment_id=assessment.id)


@blueprint.route('/<assessment_id>/bulk_action', methods=("POST",))
@login_required
def bulk_action_finding(assessment_id):
    data = request.form.to_dict()
    action = data.pop('action', None)
    data.pop('csrf_token', None)
    data.pop('finding:all', None)

    assessment = Assessment.query.filter_by(id=assessment_id).one()
    if not current_user.audits(assessment):
        abort(403)

    set_findings = set()
    for k, v in data.items():
        if k.startswith('finding'):
            try:
                set_findings.add(int(k.split(':')[1]))
            except:
                continue

    target = Finding.query.filter(
        and_(Finding.id.in_(set_findings), Finding.assessment == assessment)
    )
    if action == "delete":
        target.delete(bulk=True)
        flash("{} items deleted successfully.".format(len(set_findings)), "success")
    elif action.startswith('status_'):
        status = None
        if action == "status_pending":
            status = FindingStatus.Pending
        elif action == "status_reviewed":
            status = FindingStatus.Reviewed
        elif action == "status_confirmed":
            status = FindingStatus.Confirmed
        elif action == "status_false_positive":
            status = FindingStatus.False_Positive
        elif action == "status_other":
            status = FindingStatus.Other

        for elem in target:
            elem.status = status

        db.session.commit()
        flash("{} items set to {} status successfully.".format(len(set_findings), status.name), "success")

    return redirect_back('.findings', assessment_id=assessment_id)


@blueprint.route('/<assessment_id>/actives', methods=("POST", "GET"))
@login_required
def actives(assessment_id):
    assessment = Assessment.query.filter_by(id=assessment_id).one()
    if not current_user.audits(assessment):
        abort(403)

    form = ActiveCreateNewForm(request.form)
    list_actives = assessment.actives
    context = dict(
        route=ROUTE_NAME,
        endpoint=request.url_rule.endpoint.split('.')[-1],
        assessment=assessment,
        actives=list_actives,
        form=form
    )

    if form.validate_on_submit():
        data = dict(form.data)
        data.pop('csrf_token', None)
        try:
            active = Active[assessment, data['name']]
        except:
            active = Active(name=data['name'], assessment=assessment)

        AffectedResource(active=active, route=data['route'])
        db.session.commit()
        return redirect_back('.actives', assessment_id=assessment_id)

    return render_template('assessments/panel/list_actives.html', **context)


@blueprint.route('/<assessment_id>/evidences', methods=("POST", "GET"))
@limiter.exempt
@login_required
def evidences(assessment_id):
    assessment = Assessment.query.filter_by(id=assessment_id).one()
    if not current_user.audits(assessment):
        abort(403)

    form = EvidenceCreateNewForm()
    context = dict(
        route=ROUTE_NAME,
        endpoint=request.url_rule.endpoint.split('.')[-1],
        assessment=assessment
    )
    if form.is_submitted():
        if form.validate_on_submit():
            upload_path = assessment.evidence_path()

            try:
                os.makedirs(upload_path)
            except FileExistsError:
                pass

            file = form.file.data
            filename = secure_filename(file.filename)
            try:
                Image(assessment=assessment, name=filename)
                db.session.commit()

                img = PillowImage.open(file)
                img.save(os.path.join(upload_path, filename))
                img.close()
            except IntegrityError:
                db.session.rollback()
                return "Duplicate image name {}".format(filename), 400
            return "OK", 200
        else:
            return "Invalid file", 400
    return render_template('assessments/panel/evidences.html', **context)


@blueprint.route('/<assessment_id>/evidences/<evidence_name>')
@limiter.exempt
@login_required
def get_evidence(assessment_id, evidence_name):
    assessment = Assessment.query.filter_by(id=assessment_id).one()
    if not current_user.audits(assessment):
        abort(403)

    image = Image.query.filter_by(
        assessment=assessment,
        name=evidence_name
    ).one()

    return send_from_directory(
        assessment.evidence_path(),
        image.name,
        mimetype='image/jpeg'
    )


@blueprint.route('/<assessment_id>/reports')
@login_required
def reports(assessment_id):
    assessment = Assessment.query.filter_by(id=assessment_id).one()
    if not current_user.audits(assessment):
        abort(403)

    context = dict(
        route=ROUTE_NAME,
        endpoint=request.url_rule.endpoint.split('.')[-1],
        assessment=assessment
    )
    return render_template('assessments/panel/reports.html', **context)


@blueprint.route('/<assessment_id>/reports/download', methods=('POST',))
@login_required
def download_reports(assessment_id):
    assessment = Assessment.query.filter_by(id=assessment_id).one()
    if not current_user.audits(assessment):
        abort(403)

    data = request.form.to_dict()
    data.pop('action', None)
    data.pop('csrf_token', None)
    data.pop('template:all', None)

    templates = set()
    for k, v in data.items():
        if k.startswith('template'):
            try:
                template_name = k.split(':')[1]
                templates.add(Template[assessment.client, template_name])
            except:
                continue

    if not templates:
        flash('No report selected', 'danger')
        return redirect_back('.reports', assessment_id=assessment_id)

    report_path, report_file = generate_reports_bundle(assessment, templates)
    return send_from_directory(
        report_path,
        report_file,
        mimetype='application/octet-stream',
        as_attachment=True,
        attachment_filename=report_file,
    )


@blueprint.route('/<assessment_id>/reports/download/<template_name>', methods=('GET',))
@login_required
def download_report(assessment_id, template_name):
    assessment = Assessment.query.filter_by(id=assessment_id).one()
    if not current_user.audits(assessment):
        abort(403)

    template = Template.query.filter_by(
        client=assessment.client,
        name=template_name
    ).one()

    report_path, report_file = generate_reports_bundle(assessment, [template])
    return send_from_directory(
        report_path,
        report_file,
        mimetype='application/octet-stream',
        as_attachment=True,
        attachment_filename=report_file,
    )
