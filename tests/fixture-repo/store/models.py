from django.db import models


class Patient(models.Model):
    full_name = models.CharField(max_length=200)
    medical_record_number = models.CharField(max_length=64)
    date_of_birth = models.DateField()
    ward_reference = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    def label(self):
        return self.full_name
