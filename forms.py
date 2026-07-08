from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, TextAreaField, BooleanField, DateField, SelectField
from wtforms.validators import DataRequired, Email, Length, EqualTo, Optional, ValidationError
import re as _re


def _password_strength(form, field):
    """Require at least one letter and one digit (length already enforced separately)."""
    pwd = field.data or ""
    if not _re.search(r"[A-Za-z]", pwd) or not _re.search(r"\d", pwd):
        raise ValidationError("Password must include at least one letter and one number.")


class SignUpForm(FlaskForm):
    full_name = StringField('Full Name', validators=[DataRequired(), Length(2, 150)])
    username  = StringField('Username',  validators=[DataRequired(), Length(3, 80)])
    email     = StringField('Email',     validators=[DataRequired(), Email()])
    phone     = StringField('Phone',     validators=[Optional(), Length(max=20)])
    password  = PasswordField('Password', validators=[DataRequired(), Length(min=8), _password_strength])
    confirm_password = PasswordField('Confirm', validators=[DataRequired(), EqualTo('password')])

class SignInForm(FlaskForm):
    email    = StringField('Email',    validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    remember = BooleanField('Remember me')

class RequestResetForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])

class ResetPasswordForm(FlaskForm):
    password = PasswordField('New Password', validators=[DataRequired(), Length(min=8), _password_strength])
    confirm_password = PasswordField('Confirm', validators=[DataRequired(), EqualTo('password')])

class BookingForm(FlaskForm):
    preferred_date = DateField('Preferred Date', validators=[Optional()])
    notes          = TextAreaField('Notes',       validators=[Optional(), Length(max=1000)])

class ContactForm(FlaskForm):
    name    = StringField('Name',    validators=[DataRequired(), Length(2, 100)])
    email   = StringField('Email',   validators=[DataRequired(), Email()])
    subject = StringField('Subject', validators=[Optional(), Length(max=200)])
    message = TextAreaField('Message', validators=[DataRequired(), Length(10, 3000)])

class JobApplicationForm(FlaskForm):
    cover_letter = TextAreaField('Cover Letter', validators=[Optional(), Length(max=3000)])
